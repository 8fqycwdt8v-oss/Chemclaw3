# D-2026-09-23-the-bound-belongs-on-the-mix-not-on-the-weight — bounding a retrieval weight

**Status:** accepted · **Date:** 2026-09-23 · Amends
`D-2026-08-01-a-cap-that-starves-a-source` by supplying the half it did not reach. Closes the
`BACKLOG.md` row *"`retrieval_source_weights` has no upper bound, and the mix it produces is not a
property of the weight alone"*.

## Context

`core/config/retrieval.py`'s validator refuses a zero, a negative and a non-finite weight, and
stops there. The row asked whether it should also refuse a large one.

**It should not, and the validator's own docstring already says why**: "a weight has no upper bound
to clamp toward". A weight divides the rank, so there is no value at which it stops meaning
something — only values at which what it means becomes surprising.

**And the surprise is not a property of the weight.** Driven at the shipped
`retrieval_fusion_k=60` over five legs (`graph 45 / lexical 8 / share 10 / vector 7 /
warehouse 12`), counting survivors by what each leg *offered*:

| weights | cut 8 | cut 30 | cut 40 |
|---|---|---|---|
| uniform | 2 / 2 / 2 / 1 / 1 | 6 / 6 / 6 / 6 / 6 | 9 / 8 / 8 / 7 / 8 |
| `{"graph": 10}` | **8 / 0 / 0 / 0 / 0** | 22 / 2 / 2 / 2 / 2 | 30 / 3 / 3 / 2 / 2 |

The same weight starves four legs at one cut and starves nothing at another. So the damage is a
function of `weight × legs × cut`, and a ceiling on the first term alone would refuse deployments
that are fine and admit deployments that are not.

**This is `D-2026-08-01-a-cap-that-starves-a-source` reached through a knob.** That decision made
truncation round-robin across sources precisely so a flat cut could not take a leg to zero, and it
left the fused path alone on the argument that RRF ranks by position and so cannot be dominated the
way a score-sorted union was. That argument was true when it was written and stopped being true
when weights arrived: a weight divides the rank, so a large enough one lifts one leg's *whole* list
above every other leg's best hit. The fused path has had no shape guarantee since.

## Decision

**Put the bound on the surviving mix, at the cut, and leave the weight unbounded.**

`retrieval/hybrid.py::with_no_leg_cut_out` reorders a fused ranking so the first `limit` entries
leave no contributing leg at zero, promoting the fewest entries that makes that true.

- **The floor is one chunk per leg.** One is exactly what round-robin's first pass gives, so this
  says what `D-2026-08-01` said, at the cut it did not reach. **Anything larger is an allocation
  rather than a guard**, and measured it acts on cases that were never starved: a floor of
  `limit // (2 × legs)` takes cut 30 from `graph 22` to 18 and cut 40 from 30 to 24. The shape of
  allocation `D-2026-08-01` actually considered — proportional to what a leg returned — it
  rejected outright, for rewarding a source that returns many weak hits.

- **Inert unless a leg is at zero**, by construction and measured. If every leg already has a
  representative inside the window, every reserved position is already inside it and the
  reordering is the identity. Nine cases were swept (three weightings × three cuts) and **exactly
  one moves**: the starved one, `{"graph": 10}` at a cut of 8, to `graph 4` and one each. The
  weight still buys graph half the window, which is what a deployment that wrote `10` asked for.
  `tests/test_knowledge_gaps.py` asserts identity on the chunk list rather than on a per-leg count,
  because a count can match while the order moved.

- **Leg membership is read from the legs' own offered lists, never from `chunk.retriever`.** The
  fusion keeps the first chunk seen for a note, so a note three legs found carries the name of
  whichever ran first; counting by that field credits earlier legs and pins later ones at zero —
  the error `fanout.record_kept_chunks` records having measured as `graph 16, lexical 0,
  vector 0`. Reading the offered lists is also what makes this hold unchanged under `corpora`,
  where `_fuse_by_corpus` relabels a representative's `retriever` to its corpus name. A test
  constructs the case where the naive reading sees two legs at zero and asserts both halves: that
  it would, and that this floor is inert.

- **Applied to the `hybrid` arm of `gather_evidence` only.** The `graph` arm is
  `_interleave_dedup`, which gives every leg its best hit before any leg gets its second — the same
  guarantee, arrived at by construction. `tests/test_hybrid_retrieval.py::test_truncation_is_fair_across_sources`
  pins that arm's exact mix, and it is untouched.

- **The weight's validator is unchanged.** It still refuses zero, negative and non-finite and
  nothing else, and it now names where the bound went.

## Consequences

**A deployment can write any positive weight and cannot silently delete a leg with it.** What a
large weight now buys is the window minus one slot per other leg, which is a mix a reader can
predict from the weight and the leg count.

**The character budget is not covered and the docstring says so.** `gather_evidence_max_chars` is a
second cut spending down the same order, so a promoted chunk far enough into the window can still
be cut by it. The promotion places each leg's best hit at its own fused position rather than at the
front, so a leg is no likelier to be cut by chars than before; what is bounded here is the count
cap, which is the cut the row is about.

**Revisit when:** a deployment reports that one guaranteed chunk per leg is not enough to make a
starved leg useful — at which point the question is a per-leg *share*, and the thing to measure
first is whether the answer changed rather than only the mix. The file that would show this
decision is wrong is `tests/test_knowledge_gaps.py`, whose identity sweep fails the day the floor
starts acting on a sweep that was not starved.
