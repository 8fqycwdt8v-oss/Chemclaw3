# D-2026-09-05-the-cut-the-gate-could-not-see — four open points, two of them measured shut

**Status:** accepted · **Date:** 2026-09-05

## Context

`D-2026-09-04-a-ranker-that-sorts-alphabetically-is-not-a-ranker` and
`D-2026-09-05-a-procedure-that-leaves-no-record` each closed with a list of what they had left. Four
rows: the per-leg cut nobody could see, RRF's `k` over short lists, a gold corpus smaller than the
bound it was meant to police, and `turn_costs` without any knowledge dimension.

Two were built. One was measured and found to have the right diagnosis and two wrong remedies. One
grew a corpus and a floor that can now fail.

## The per-leg cut

`EvidenceSweep` exists so "a cut does not look like a corpus". That was true of the merge and false
of the legs: `truncated_by` names which merge cap fired, and neither it nor `total_before_cap` can
see a retriever that truncated before handing anything over. On the shipped configuration that is
the only cut that ever bites — `retrieval_top_k` is 8 against caps of 40 chunks and 60,000
characters, so with three text legs the merge bound is unreachable. Re-measured on 5,000 notes that
all matched every term: `chunks=8, total_before_cap=8, truncated_by=None`, with 4,992 discarded
inside the graph leg.

**The carrier is a `list` subclass, and that is the decision.** 84 call sites consume `retrieve()`
and **81 are tests**; changing the return type churns all of them for no behavioural gain anywhere.
A mutable attribute on the retriever is wrong because one instance serves concurrent turns, and
repeating the count on every chunk has nowhere to put it when a source returns none. `Hits` *is* a
list, so only the two ends that care changed.

`found is None` means the source cannot say, which is not "did not cut": the graph leg scores
everything and then truncates, while the dense and lexical legs push `LIMIT k` into the index and do
not know what they did not fetch. A zero for them would assert completeness from the one leg that
cannot check it.

## RRF: the row's diagnosis was right and both its remedies are no-ops

The arithmetic reproduces exactly. At `k=60` over lists of 8 the within-source spread is **1.11x**
against **2.00x** for being found twice, so a two-source note at rank *r* beats a one-source rank-1
note while `r < 62`. End to end the answering note moves from position 2 in `graph` mode to position
9 of 9 in `hybrid`.

**Neither `k` nor `retrieval_source_weights` can close it, and that is arithmetic.** The agreement
term contains no `k`: two legs at rank 1 score `2/(k + 1/w)` against a dense-only rank-1 note's
`1/(k+1)`, and the first wins for every positive `k` and `w`. Measured, lowering `k` to 20 or 10
changes the order on **0 of 7** real queries; at `k=1` the answer note reaches position 6, still
below every two-source note; tiering graph+lexical at 0.5 leaves it at 9.

The correlation is also worse than the row said: on the real corpus `graph ∩ lexical` is 47/55 and
`graph ∩ vector` 44/55, because the shipped `embedding_provider` is `hash` — token-count hashing —
so all three legs are term-overlap rankers and the dense one is not orthogonal at all until a site
configures a real embedder.

So **no `k` change ships**. What ships is the one clause that was a defect: the fused list carried
each chunk's *finder's* score — a `confidence`, a `ts_rank` and a cosine in one column — monotone
with the fused order on **0 of 7** queries. `restated_as_position` reports the only quantity the
fusion produced, and `ingest/documents/retriever.py` had already made that restatement over its own
two legs, so extracting it removes a duplicate rather than adding an abstraction.

Not applied to `graph` mode: round-robin preserves each source's ordering, so a chunk's own score
still explains its position, and a blanket restatement would delete KM-5's truncation signal for
every deployment that has not opted into hybrid — which is all of them.

## A gate that could not fail

The gold corpus held **6** notes against a `retrieval_top_k` of 8, so the per-leg cut could not
engage and 4 of 5 cases sat at recall 1.00. Its own docstring says it exists so a change "could not
quietly halve recall unnoticed"; it could not detect the only way recall halves.

It is now 48 notes and 10 cases over one coherent programme, with distractors that genuinely match
the query terms — a distractor that cannot match teaches the gate nothing. Verified rather than
asserted: neutralising the BM25-lite relevance so the sort falls back to the pre-2026-09-04 ordering
takes four of the ten cases red, two of them to **0.000**, including the case whose gold note sorts
last in the corpus. The same revert moves **nothing** on the six-note fixture.

Incidentally found and fixed: `validate_kg` reported **6 layout problems** on that corpus — one per
note, flat files where the PR-gate files `<type>/<id>.md` — on a directory whose own README called
every file a valid note.

**The floor moves 0.75 → 0.80, and the number is derived rather than chosen.** A case with four gold
notes scores exactly 0.75 when one is lost, so it *passed* a floor of 0.75 — and four of the nine
gated cases have four gold notes, so on nearly half the set the floor was blind to the smallest
regression that exists. The floor must sit strictly above `max((n-1)/n)`; 0.80 is the lowest round
value above it at n=4. `tests/test_retrieval_eval.py` asserts that inequality against the loaded
cases rather than restating 0.80, so a future case with a five-note gold set fails loudly instead of
silently reopening the blind spot — and it reads the *shipped default* off the model field, because
the corpus fixture pins the setting and a test that read the pin would assert nothing.

## The turn nobody could count

`turn_costs` recorded what a turn spent and how it ended, and could not say whether the turn
consulted the record at all — the question two reviews of this loop each answered with a bespoke
script, and which is not recoverable afterwards because the event stream ends with the turn.
Migration 082 adds `retrieval_calls`, `capture_calls`, `answer_confidence`, `review_required` and
`notes_cited`, counted in `note_event`, which is already "the one place the counts are taken".

Capture is classified off `side_effecting_tools()` so a bundle's new write tool lands on the right
side the day it is enabled. Retrieval cannot be: authz partitions by what may run without approval,
and counting by it would book `ask_clarifying_question` as a search. `KNOWLEDGE_READ_TOOLS` is a
stated subset with a test holding it inside `READ_ONLY_TOOLS`.

`answer_confidence` is nullable and must stay so. The verifier can be off, and the answer-shape gate
sets `review_required` with no score at all; a 0 there would read as "graded, and graded terrible"
for a turn that was never graded.

## Consequences

- `retrieval_recall` baseline 0.90 → 0.95 and `retrieval_precision` 1.00 → 0.476. The precision drop
  is not a regression: on a six-note corpus every hit was gold because the corpus *was* the gold set.
- `EVAL_CASE_SET_VERSION` changes, which is the mismatch tripwire doing its job.
- The context floor moves 43,316 → 43,333 — **+17 tokens for the whole change**, paid for by
  trimming developer rationale out of `gather_evidence`'s model-facing schema after the per-tool
  ceiling caught it at 1,059 against its 900 limit and said, in as many words, not to whitelist it.

## A process failure worth recording

A subagent commissioned to *design* the RRF fix edited the working tree, and a commit taken while it
was running swept ~40 unreviewed lines into a commit whose message described none of them. The code
was good and is kept; the commit was unwound and rewritten to describe everything it contains, and
the tests that change should have had were written afterwards. **A design task that can write to the
tree is not a design task** — the lesson is for whoever next dispatches one, and it is why this ADR
says which lines came from where.
