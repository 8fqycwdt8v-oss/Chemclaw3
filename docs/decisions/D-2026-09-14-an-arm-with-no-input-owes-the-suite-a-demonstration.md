# D-2026-09-14-an-arm-with-no-input-owes-the-suite-a-demonstration — `kg-validate`'s two store-backed halves

**Status**: accepted

## Context

`make kg-validate` has two halves. The pure half reads a notes directory; the other asks a database
two existence questions — does this `[[reaction-*]]` citation name a row in `reaction_records`, and
does this `calc_ref` name a key the calculation cache holds. The second half matters more than it
looks: `kg.graph.dangling_links` deliberately ignores every `reaction-` target since
D-2026-08-25, because the graph cannot see the store, so this gate is the only thing between a
typo'd run id and a merge.

Measured on the shipped tree: `0 reaction citation(s) and 0 calc_ref(s) verified`. Both arms are
dead on every CI run.

## The row proposed giving the corpus input, and a merged test forbids it

The obvious fix is to put a citation in the shipped corpus. It is not available, and the reason is
already recorded one file over:
`tests/test_seed_corpus.py::test_the_seed_corpus_cites_no_calculation_the_store_cannot_back`
**requires** the seed corpus to cite no calculation, deliberately — a seed `calc_ref` is a
fabricated key, and the existence gate would then fail on every fresh database. The reaction half is
the same shape one step out: a committed note citing `[[reaction-EXP-1]]` needs a `reaction_records`
row, and CI's database holds migrations and nothing else.

So the corpus cannot be the input, and a gate whose input cannot exist still owes the suite the
thing `D-2026-09-14-a-gate-nothing-has-failed-is-a-gate-that-cannot-fail` makes a gated metric owe
its case-set: **a case that makes it fail.**

## What was measured

The pieces under each arm were unit-tested — `unresolved_citations` in `test_reaction_records.py`,
`unresolved_calc_refs` in `test_knowledge_gaps.py`. The **entrypoint's** arms were not: nothing
anywhere drove `validate_kg.main` over a note carrying a `calc_ref`, in either direction, so that
branch's store construction, its `try`, and its contribution to the exit code had never executed.
The reaction arm had exactly one end-to-end test, of the *unreachable-store* path, which asserts
the gate fails when it cannot look — not that it fails when it looks and finds nothing.

## Decision

`tests/test_kg_validate_store_arms.py` drives both arms through `main`, over a real Postgres, in
both directions: a citation no record backs exits 1 naming the note and the id; a citation a record
backs exits 0 and the success line *counts* it. Same for a `calc_ref` against the cache.

And `make kg-validate` now says, on a corpus that cites neither, that the two store-backed halves
had nothing to check, naming the file that drives them. Not an error — a corpus with no external
citations is legitimate — but a success line that reads like a whole gate is the shape
`map_to_hpc_identity` is remembered for, and this one has read that way on every CI run since it
was written.

## Consequences

- The shipped corpus's two zeros are now asserted rather than remembered
  (`test_the_shipped_corpus_still_gives_both_arms_nothing`), with instructions to delete that test
  if the corpus ever gains a real citation — at which point the arms have input and this file's
  premise is gone.
- These tests need Postgres and skip without it, which `tests/conftest.py` counts. That is the
  right trade: an in-memory store answers a different question than "does this row exist".

## What keeps it true

- `tests/test_kg_validate_store_arms.py::test_the_reaction_arm_fails_on_a_citation_no_record_backs`
  and `::test_the_calc_arm_fails_on_a_ref_no_calculation_produced` — the demonstrations. Driven:
  dropping either arm's `problems.extend(...)` reddens exactly its own test and nothing else.
- `::test_the_reaction_arm_passes_on_a_citation_a_record_backs` and
  `::test_the_calc_arm_passes_on_a_ref_the_cache_holds` — the other direction, so neither
  demonstration is satisfied by an arm that refuses everything.
- `::test_a_corpus_with_no_citations_says_the_store_halves_did_not_run` — the operator-facing half.
