# D-2026-09-14-one-corpus-one-vote-is-the-right-fix-for-a-different-problem — the RRF correlation row, measured a third time

**Status**: accepted

## Context

`BACKLOG.md` carried a thoroughly measured row: RRF's premise is independent rankers, `graph`,
`lexical` and `vector` are three rankers over one note tree, and the two dials that look like the
remedy — `retrieval_fusion_k` and `retrieval_source_weights` — are measured no-ops. It ended with a
proposal: *"What would work is 'one corpus, one vote' … expressing that means the data-source
manifest saying which sources are one corpus, which is an ADR rather than a setting."*

This is that ADR, and the proposal is a **third** measured no-op for the symptom it was proposed
against. It is a correct fix for a different problem, and it ships on that basis.

## What was measured

The measurement is new: W27.3's gold set made it possible. **20 real probe questions, 46 labelled
(query, note) pairs, the shipped `knowledge/` corpus (39 notes indexed), all three legs live.**
Where each expected note lands in the merged list:

| merge | gold notes retrieved | mean rank | median | top-3 | top-5 |
| --- | --- | --- | --- | --- | --- |
| round-robin (`retrieval_mode=graph`, the shipped default) | 39/46 | **4.38** | 3.0 | **21** | **27** |
| RRF, single-stage (`hybrid`, before) | 39/46 | 4.54 | 3.0 | 20 | 25 |
| RRF, one corpus one vote (`hybrid`, after) | 39/46 | 4.54 | 3.0 | 20 | 25 |

**Two findings, and the second is the one that decides something.**

**The remedy moves 0 of 46 gold ranks**, and the reason is structural rather than a tuning miss:
with the three legs grouped into one corpus, the cross-corpus stage has exactly one list, so the
final order *is* the within-corpus fusion — which is the single-stage RRF over the same three lists.
Grouping correlated legs protects *other* corpora from them; it does nothing about the crowding
inside the group, which is what the row measured. Re-run with `vendored` enabled as a fourth source:
still 0, because a reagent-reference corpus contributes nothing to a question about a Suzuki
coupling.

**RRF is measurably worse than the round-robin the default already uses**, on this corpus: 12 gold
notes rank worse against 17 better, and the losses are concentrated where they matter —
`playbook-degassing` 1 → 7 for `kn-02`, `opt-suzuki-conditions` 3 → 9 for `kn-01`,
`report-biaryl-development` 2 → 7 for `kn-06`. Every one of those is a note the question is directly
about, pushed down by notes three correlated legs all agree on. That is the correlation defect
end to end, on labelled data, for the first time.

## Decision

**Ship the mechanism, on its own merit rather than on the row's.** `DataSourceManifest.corpus`
names the body of evidence a source reads; `reciprocal_rank_fusion(..., corpora=...)` fuses within a
corpus and then across corpora; `graph`, `lexical` and `vector` declare `corpus: knowledge-notes`.
What it buys is the case the row's arithmetic actually proves: a note corpus read by three legs
cannot outvote a single-leg corpus — a mounted share, an ELN warehouse, Pistachio — on agreement it
generated with itself. That configuration is one environment variable away and the fusion gets it
wrong today; the unit test drives it and the order changes.

It is **not** dead code and it is not a guard with no caller: `research_tools` passes the corpora on
every hybrid sweep, and `ingest/documents/retriever.py` has always done the same thing internally
for the mounted share, which is the second caller that made the rule worth naming rather than
inlining.

**Do not recommend `hybrid`.** `retrieval_mode` stays `graph`, and the reason is now a number rather
than caution: on labelled data RRF is worse than round-robin over this corpus, and it stays worse
after the fix. The remaining honest options are to make the legs genuinely independent (an
`openai_compatible` `embedding_provider` makes the dense leg orthogonal — the shipped `hash`
provider is token-count hashing, which is why all three are term-overlap rankers) or to stop running
three legs over one corpus at all. Both are decisions with a cost, and neither is this one.

## Consequences

- A deployment enabling two corpora under `hybrid` gets a different, better order. Every
  configuration with one source per corpus — which is every shipped one — fuses byte-identically,
  asserted rather than assumed.
- The corpus's cross-corpus weight is the **mean** of its sources' weights, which is exactly the
  source's own weight when a corpus has one source. That is the generalisation that leaves the
  existing case alone.
- The `BACKLOG.md` row is replaced rather than deleted: its remedy is closed, and what it was about
  is not.

## What keeps it true

- `tests/test_hybrid_rrf.py::test_three_legs_over_one_corpus_vote_once` — three legs of one corpus
  against one leg of another; single-stage puts the three-leg note first, two-stage does not.
  Driven: deleting the two-stage branch reddens it.
- `tests/test_hybrid_rrf.py::test_all_distinct_corpora_fuse_exactly_as_before` — the other
  direction, so the change cannot be satisfied by re-ranking everything.
- `tests/test_hybrid_rrf.py::test_a_corpus_list_that_does_not_match_the_ranked_lists_is_refused` —
  the mapping is positional.
- `tests/test_datasource_seam.py::test_the_three_note_legs_declare_one_corpus` — the assertion the
  unit tests cannot make, because they build their corpus list by hand. Driven: commenting
  `corpus: knowledge-notes` out of all three manifests left every fusion test green and reddens
  this one.
- `tests/test_datasource_seam.py::test_a_source_that_declares_no_corpus_is_its_own` — driven:
  answering one corpus for everything reddens it.
