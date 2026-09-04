# Knowledge capture, storage and retrieval — deep review and fixes

Six fresh-context reviews (capture coverage, PR-gate/graph, storage, retrieval mechanics, the
agent-side loop, measurement) against a live Postgres/Temporal stack. What they found is not a
missing feature: **every mechanism in this loop exists and most of them are well argued. The loop
does not close because the pieces disagree with each other, and nothing measured the disagreement.**

The one-line answer to the question asked: *data is captured automatically; conclusions are not —
and what is captured is retrieved by a ranker that sorts alphabetically and reports no truncation.*

## Tier 1 — retrieval returns the right thing

- [x] 1. Graph leg has no relevance signal. `retrievers.py:246` sorts `(-coverage, -confidence, id)`
      and `confidence` defaults to 0.5 for every note, so on tied coverage the ranking is
      **alphabetical**. Measured: 5,000 notes match, 8 returned, ids `reaction-00000..00002`.
- [ ] 2. That cut is invisible. `total_before_cap` is computed after the merge, so `gather_evidence`
      reports `truncated_by=None` while 4,992 matching notes were dropped inside the retriever.
      The two caps wired to `truncated_by` cannot bite (max 24-34 chunks against a cap of 40).
- [x] 3. One oversized note bricks both derived legs, silently, forever. `vector_index.py:674`
      embeds `search_text(note)` whole with no guard and upserts the whole changed set after one
      `embed_texts`. Measured: a 989 kB note raises, notes indexed after the failure = 0. The
      tsvector is written in the same INSERT, so the lexical leg freezes too.
- [ ] 4. A ranked evidence list is cut head-and-tail by bytes. Measured at batch width 4 the sweep
      keeps ranks 0-3 and 27-29 — it retains the three *worst*-ranked chunks and drops the middle.
- [x] 5. The lexical reference and Postgres disagree on chemistry numerics. `-78 °C` indexes as the
      lexeme `-78`, so a query of `78` misses it and `-78` reads as "exclude 78": a cryogenic
      temperature was reachable by no query at all. Decimals diverge the other way. Fixed by
      `normalize_search_text` applied on **both** sides — document-side only would have broken CAS
      lookup, which works today only because query and document fragment identically.
- [x] 6. `reaction_records` lost its `reaction_id` index when migration 056 moved the PK to
      `(ingest_source, reaction_id)`. Measured at 500k rows: `expand_note`'s read is a 38.9 ms full
      index scan (0.10 ms with the index); `eligible()` at the shipped depth is a 76.3 ms parallel
      seq scan (1.1 ms with it).

## Tier 2 — the gate that should have caught Tier 1

- [ ] 7. The retrieval gate scores a **6-note** corpus — smaller than `retrieval_top_k` — so the cut
      can never engage. Adding 30 distractor notes that sort earlier, with no code change, takes
      `retrieval-coupling` from 1.00 to 0.25 and `retrieval-suzuki` from 1.00 to 0.50.

## Tier 3 — gate and graph correctness

- [x] 8. A rejected note is silently re-pushed and a later merge mislabels the record forever.
      `pr_gate.propose_note:157` submits to git *before* consulting the store; the store refuses to
      reopen the row but the branch stays live and mergeable, and `mark_merged` moves only OPEN
      rows — so `close_merged_notes` moves 0 and the trail says "rejected" for a note now serving
      as evidence.
- [x] 9. A transient parse failure deletes derived-index rows. `reindex_notes:662` builds `keep`
      from the *parsed* set while `_parse_notes` skips unparseable notes by design. Measured:
      40 of 100 notes unparseable deletes 40 index rows, which must then be re-embedded.

## Tier 4 — the agent looks, and keeps what it found

- [x] 10. Retrieval is 100% model discretion and the prompt *describes* it. The strong form exists
      exactly once, for safety. Give retrieval an obligation in that same shape.
- [x] 11. Compaction clears the `gather_evidence` sweep **first** (upstream is oldest-first) —
      measured, 3 tool calls at the ceiling and it is gone before the model writes the answer.
      Preserve the citation index in the placeholder.
- [x] 12. `HELPER_BRIEF` never tells the helper to carry note ids, so a caller cannot cite what its
      helper read.
- [x] 13. `recall_preferences` is never named in the system prompt — a built, durable, per-actor
      cross-session layer the model does not know exists.

## Tier 5 — measurement, so none of this can regress silently

- [x] 14. `chemclaw_evidence_source_kept_total` reads **0.00 for every non-first source** on a
      healthy corpus: `kept` is attributed by `chunk.retriever` and both merge paths keep the first
      occurrence. The one metric built to detect a starved source is pinned at zero.
- [ ] 15. Bind the corpus census (`GraphGaps`, already computed, 0.71 ms) as a gauge family, and
      meter the sweep outcome.

## Deliberately not in this change (opened as BACKLOG rows instead)

RRF `k=60` over 8-item lists (needs a tuning decision plus the graph/lexical double-count question);
the O(corpus) `git worktree add` in the PR-gate (2.9 s/proposal at 10k notes — a real fix, but it
replaces a security-relevant containment check); `random_page_cost` and the dense leg's plan flip
(a deployment tuning requirement, not code); the runbook's restore inventory; end-of-turn
distillation and template-job `record_job` (capture, a larger change than this one).


## Review

**Done and pinned by a test that fails on the code it replaces** (items 1, 3, 5, 6, 8, 9, 11, 14):
the graph leg's relevance ranking (3/3 answering notes where the same fixture returned 0/3), the
per-note embed bound and batching, the numeric tokenisation on both sides, migration 081, the
rejected-note guard, the reindex retirement guard, the citation-preserving compaction placeholder,
and the kept-metric attribution (an existing probe now reads 25/8/7 where it read 16/0/0).

**Done, no test possible** (items 10, 12, 13): the prompt's retrieval and capture obligations and
`HELPER_BRIEF`'s citation rule are model instructions. `tests/test_context_floor.py` measures what
they cost — 42,730 to 43,063 against an unraised 43,500 ceiling — and `tests/test_subagents.py`
already asserts the helper brief and the `task` description agree on the bounds each states. Whether
the obligations *work* needs the live probe lane against a model, which is the row this repository
already has for it.

**Not done, and each is now a `BACKLOG.md` row carrying its measurement** (items 2, 4, 7): the
`retrieval_top_k` cut is still invisible, RRF's `k` is still 60 over 8-item lists, and the gold
corpus is still smaller than `retrieval_top_k`. Item 2 is a contract change across four retrievers,
the fan-out and the harness; the two shapes that avoid it — a mutable attribute on a retriever
shared across concurrent turns, or the count repeated on every chunk — are both worse than the gap,
so it is queued rather than half-fixed. Item 4 (the head-and-tail byte cut over a ranked list) is
folded into that row: cutting by rank is the same seam.

**One thing worth recording as a near-miss.** The first version of the numeric fix normalised only
the *document* side. It passed every test I had written and would have broken every CAS and
lot-number lookup in the corpus, because those work today precisely by the query and the document
fragmenting identically — a property one review measured as a *negative* finding while another was
writing the fix. Measuring before keeping it is the only reason it did not ship.
