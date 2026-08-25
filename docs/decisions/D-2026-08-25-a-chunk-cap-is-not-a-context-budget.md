# D-2026-08-25-a-chunk-cap-is-not-a-context-budget — the evidence sweep gains a second bound and a way to say it was cut

**Status:** accepted · **Date:** 2026-08-25 · Extends
[`D-2026-08-01-a-cap-that-starves-a-source`](D-2026-08-01-a-cap-that-starves-a-source.md), which
fixed the *shape* of this cut and left its *currency* alone.

## Context

`gather_evidence` capped its result at `gather_evidence_max_chunks` = 40. That is a count, and the
chunks it counts are not comparable:

| source | chunk size | 40 chunks |
|---|---|---|
| a note-backed retriever | `note_excerpt_chars` = 240 | ~9.6 kB |
| a mounted share | its binding's `chunk_chars` = 1,800 | ~72 kB |

A **7.5×** spread with nothing normalising it, against `agent_context_token_budget` = 100,000. This
is the finding `agent_keep_last_conversation_groups` already records in this tree, in as many words:
*a count of groups cannot bound anything, because what a group costs is whatever was said in it* —
measured there at a 300k-token thread reduced to 180k against a 100k budget.

Two silences rode on the return type, and the tool's own comment had already named the second and
deferred it: *"closing that needs the return type to carry provenance, which is a contract change
beyond this fix"*.

- **A cut looked like a corpus.** Hitting the cap returned a short `list[EvidenceChunk]`, identical
  in shape to a small corpus — while the tool's docstring tells the model that empty means "nothing
  on file, never invented".
- **A partial outage looked like a partial corpus.** `gather_evidence` raises when *every* source
  fails, correctly. When one of four fails it returned real-but-incomplete evidence with the
  degradation visible only on the stream, so a chemist reading the result could not tell it from a
  corpus that genuinely holds that much.

## Decision

**`gather_evidence_max_chars` is added as a second bound; the count is kept.** Both apply. Keeping
the count is `agent_keep_last_tool_groups`' argument — it is ENV-visible and deployments set it, so
refining what it means beats replacing it. 60,000 characters is ~15k tokens: a graph-only deployment
is byte-identical, and a share-heavy sweep stops at roughly 33 chunks.

**Spent by walking the merged ranking, which is what keeps the new bound fair.** D-2026-08-01 is
about the *shape* of a cut rather than its size: `ranked` is already round-robin across sources (or
RRF-fused), so consuming it in order spends characters cross-source-fairly for the same reason it
spends slots that way. A second cap applied per source, or over a re-sorted union, would reintroduce
the starvation that ADR measured to zero surviving chunks on a whole leg.

**At least one chunk always survives**, so an over-budget first chunk cannot produce an empty list —
which this tool's contract reads as "nothing on file". The same clamp
`KeepLastConversationGroupsEdit` makes, for the same reason.

**The return type becomes `EvidenceSweep`**, carrying `chunks`, `truncated_by`, `total_before_cap`
and `sources_failed`. This is the contract change the tool's comment deferred, and it closes both
silences at once: the model is told which bound bit and how much there was, and is told when a
source could not be asked. `truncated_by` distinguishes `count` from `chars` because they are
separately actionable — a count cut narrows with a filter, a character cut means the sources are
returning long chunks and a narrower question will reach further.

## Consequences

- Twelve call sites read `.chunks`. Nothing in `evals/` or `api/` consumed the return value —
  they reference the tool in prose only — so the blast radius is the tests and one example.
- `skills/deep-research` gains the rule that matters: read what the sweep says about itself before
  reading the chunks, because an outage and a cut both look like an absence in the chunk list
  alone, and reporting either as "we have no prior art" is a confident claim about a question that
  was never fully asked.
- `_interleave_dedup` and the merge modes are untouched. This changes how much of the ranking is
  kept, never its order.

## What the tests do and do not prove, measured

`test_the_character_budget_does_not_starve_a_source` was written against mutants rather than
asserted:

| cut applied | surviving | test |
|---|---|---|
| in config order (the original D-2026-08-01 shape) | `{"graph": 12}` — share starved to zero | **fails**, correctly |
| re-sorted by score | both legs survive at this budget | passes |
| in merged-rank order (shipped) | both legs survive | passes |

So what it pins is the **currency** change specifically. The score-re-sort shape is guarded by
`test_a_mounted_share_is_not_starved_by_a_larger_graph` and by the cross-source sort being gone from
`_interleave_dedup`. That limit is written into the test rather than left for someone to discover
the test was weaker than it read — a one-sided assertion would also have missed the mirror image,
since the share's RRF-derived 1.0 outranks a note's 0.5 confidence and a score-re-sorted cut starves
the *graph* instead.
