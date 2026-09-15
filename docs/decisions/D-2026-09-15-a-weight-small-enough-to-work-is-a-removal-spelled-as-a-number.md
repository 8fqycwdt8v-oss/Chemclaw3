# D-2026-09-15-a-weight-small-enough-to-work-is-a-removal-spelled-as-a-number — A weight small enough to work is a removal spelled as a number

**Status:** accepted · **Date:** 2026-09-15 · **Commit:** the fourth remedy for the RRF correlation
row, measured. Supersedes nothing; it adds a fourth measured no-op to
`D-2026-09-14-one-corpus-one-vote-is-the-right-fix-for-a-different-problem`, which is merged and
therefore not edited, and it keeps the instrument all four were found with.

## Context

`BACKLOG.md` carries a row that has been measured three times. RRF's premise is independent rankers;
`graph`, `lexical` and `vector` are three rankers over one note tree (pairwise agreement 47/55,
44/55, 41/53, because the shipped `embedding_provider` is `hash`); so a note two legs agree on
displaces the note the question is about. Three remedies are recorded as no-ops:
`retrieval_fusion_k`, `retrieval_source_weights` tiering the strong legs *up*, and
one-corpus-one-vote. The row ends by naming what is left: *an `openai_compatible` embedding
provider, which makes the dense leg genuinely orthogonal and is the thing to measure next, or not
running three legs over one corpus.*

Two things were true of that and neither was written down. The **fourth** remedy a reader reaches
for — down-weighting the *correlated* leg rather than tiering the strong ones up — had never been
run. And the second option the row names had never been run either: nobody had measured what
dropping a leg actually does.

An embedding provider could not be measured here and that is said plainly rather than faked: this
environment carries a credential but no gateway, and the vendor behind it serves no `/embeddings`
route, so the only available `openai_compatible` embedder is `cli/mock_llm.py`'s — which would
measure the mock, not the question.

## Measured

`make retrieval-arms`, 20 probes, 46 labelled (query, note) pairs, the shipped `knowledge/` corpus,
all legs live:

| arm | found | mean rank | median | top-3 | top-5 | up | down | lost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| round-robin, 3 legs (shipped) | 39/46 | 4.69 | 3.0 | 20 | 25 | — | — | — |
| RRF, 3 legs | 39/46 | 4.72 | 4.0 | 19 | 26 | 17 | 12 | 0 |
| RRF, 3 legs, `vector` at 0.5 | 39/46 | 4.56 | 4.0 | 19 | 27 | 19 | 11 | 0 |
| RRF, 3 legs, `vector` at 0.1 | 39/46 | 4.67 | 4.0 | 18 | 27 | 19 | 12 | 0 |
| **RRF, 2 legs (graph+lexical)** | **36/46** | **3.69** | 3.0 | **21** | 26 | **20** | **4** | **3** |
| RRF, 2 legs (graph+vector) | 36/46 | 5.00 | 3.5 | 18 | 23 | 8 | 15 | 3 |

**The baseline itself has moved**, and that is worth stating because the previous ADR's figures are
still quoted: round-robin measured 4.38 / top-3 21 / top-5 27 on 2026-09-14 and measures 4.69 / 20 /
25 today, with no retrieval code changed — the corpus grew. Which is
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` again, and the reason the instrument is
now a `make` target rather than a paragraph.

### Finding 1 — down-weighting the correlated leg is the fourth no-op

Mean gold rank goes 4.72 → 4.56 → 4.67 across weights 1.0 / 0.5 / 0.1; top-3 *falls* from 19 to 18;
up/down stays 19/11–12. Nothing in the range a person would try does anything.

The reason is arithmetic and it is in `reciprocal_rank_fusion`'s own docstring, written for a
different purpose: **a weight divides the rank, and the rank term is nearly flat at `k=60`.** A
rank-1 hit contributes `1/(60 + 1/w)`, which falls only from 0.01639 to 0.01429 as `w` goes 1.0 →
0.1 — a 13% change across a tenfold weight, against a vote that has to be given up entirely.

Solving for where it *would* work puts the crossover at **`w < 2.7e-4`**: the dense leg's rank-1 hit
must fuse as though it were rank **3,729**. `retrieval_source_weights` accepts that — it refuses
non-positive weights and nothing else — so the dial can reach the behaviour. It reaches it by being
a removal written as a number. That is the decision in the title: the setting is not inert, it is
impractical, and those are different findings with different consequences.

### Finding 2 — dropping the dense leg is the first configuration to beat the default, and is still not the answer

RRF over `graph`+`lexical` is a **full rank** better than the shipped merge: mean 3.69 against 4.69,
20 gold notes up against 4 down, top-3 21 against 20. Nothing measured before this had beaten
round-robin at all.

It loses **3 of 39** gold notes outright — `kn-01|opt-suzuki-conditions` (baseline rank **3**),
`kn-14|failure-dcm-amide-coupling`, `kn-14|playbook-pd-cross-coupling-scope`. Checked for a cap
artefact and it is not one: `retrieval_top_k` is per leg and `gather_evidence_max_chunks` is 40,
which two legs at 8 never approach. Those three notes are found *only* by the dense leg.

`retrieval_recall` is the **gated** retrieval metric and rank is the diagnostic — `evals/retrieval.py`
says so, and the reason is that missing a relevant note is the failure the system exists to avoid.
So the trade goes the wrong way and the leg stays.

### Finding 3 — the correction this makes to the row's own framing

The row reads as though `hash` makes the dense leg a redundant term-overlap ranker. It does not.
Under `hash` the dense leg is a *differently weighted* term ranker — token-count hashing with a
cosine, against BM25-lite and substring — and it reaches three labelled gold notes the other two
legs never return, one of which the shipped configuration puts at rank 3. Measured separately over
eight free-text questions, it contributes 27 of 98 delivered chunks and reorders 7 of 8.

"Correlated" and "redundant" are not the same claim, and only the first is supported.

## Decision

1. **`retrieval_mode` stays `graph`.** Unchanged, and now for a fourth reason rather than a third.
2. **The dense leg stays.** Dropping it is the first measured rank win and it costs gated recall.
3. **`retrieval_source_weights` is not the remedy**, and the reason is recorded as a threshold
   rather than as "it did not help" — `tests/test_hybrid_rrf.py` asserts both sides of the 2.7e-4
   crossover, so the claim that the dial is impractical cannot decay into the claim that it is inert.
4. **The instrument is kept.** `make retrieval-arms` reproduces the table above. Four sessions have
   now built this measurement and thrown it away, and the fifth question — an orthogonal embedding
   provider — is already named in the row, so it has its second caller before it ships.
5. **An `openai_compatible` embedding provider remains the open option**, unmeasured here for a
   stated reason rather than deferred for a vague one.

## What keeps it true

- `tests/test_hybrid_rrf.py::test_a_weight_in_any_range_a_person_would_try_cannot_undo_a_correlated_leg`
  — the sweep 1.0 → 0.001, as the minimal three-note shape the row describes. Nothing exercised the
  `weights=` path in that file before this, which is why the property was available to be believed
  either way.
- `tests/test_hybrid_rrf.py::test_the_weight_that_would_work_is_small_enough_to_be_a_removal`
  — both sides of the crossover (3.0e-4 leaves the order, 2.0e-4 flips it) plus the removal, so
  "impractical" cannot be read as "inert".
- `make retrieval-arms` (`src/chemclaw/cli/retrieval_arms.py`) — the table, on demand, against
  whatever the corpus is on the day it is run. Needs `make up`: an arm naming `vector` reindexes the
  shipped corpus first, which is the dependency that kept this measurement deferred and is not a
  reason any more.
