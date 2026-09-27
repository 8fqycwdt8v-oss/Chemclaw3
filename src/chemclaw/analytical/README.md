# `analytical/` — what a result has to be, and whether a measured one is

The analytical tier. `protocols/` is prescriptive about *what to run*; this is prescriptive about
*what comes back*.

| Module | What it holds |
| --- | --- |
| `specification.py` | `AcceptanceCriterion`, and `evaluate` — the deterministic verdict a set of measurements earns against a specification. |
| `stability.py` | `estimate_trend` — where a trending attribute's one-sided 95% bound meets that same criterion, bounded by what ICH Q1E permits extrapolating. |

## The rule this package is arranged around

**No judgment and no instrument model.** Every function reads numbers a person measured and answers
from unit arithmetic. Nothing here decides whether to release a batch, whether to open an
investigation, or what a chromatogram means — those are a chemist's, and two of them are what
`SpecificationResult` exists to hand over rather than pre-empt.

## Why the verdict is not a boolean

A specification check written as "compare each result to its limit" has nowhere to put the two
answers that are not pass or fail, so both become **pass**:

- **A criterion nothing measured.** A twelve-row specification with nine results has three
  unanswered questions. Scored as a filter over the results it has nine passes and looks tested.
- **A result the limit cannot be compared with.** An area percent against a weight-percent limit is
  the same unit, the same dimension and a different fact. A dimension check alone cannot see it.

So `evaluate` returns one row per **criterion**, in the specification's order, with four verdicts:
`within`, `outside`, `not_measured`, `indeterminate`.

## The flag beside the verdict

A result of 0.48 ± 0.05 % against a 0.50 % maximum is inside the specification and
indistinguishable from outside it at the method's own precision. That is what an analyst escalates,
and a bare comparison reports it as a clean pass.

`SpecificationResult.limit_within_uncertainty` says so **without changing the verdict**: whether the
number is under the limit is arithmetic, and whether to investigate is a judgment. A result that
reported no uncertainty never sets it — "nobody said" is not "the spread is zero", which is the
distinction `core/units.Measurement.uncertainty` already makes.

## Where `Measurement.compare` fits

This package is the caller that method was written for and did not have. Its docstring said the
refusal across dimensions is the point because "a specification check written that way passes a
batch that is out of limits" — a present-tense claim about a check that existed nowhere in `src/`,
which is the shape `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` names.
`tests/test_specification.py` holds the caller in place with an absence test, because every
behavioural test here would still pass if `_score` were rewritten to compare floats itself — and the
claim would be false again, silently.

## What `stability.py` will not say

It answers the question that follows a specification check — *when* does the trend reach the limit —
and it is **not** a shelf life or a retest period. ICH Q1E derives those from a procedure this
implements one step of: the poolability testing across batches (§2.3's ANCOVA at the 0.25 level),
the choice of worst-case batch, and the judgment about whether a linear model suits the attribute
are all outside a single batch's regression. Every result carries that sentence, on both the
crossing and the non-crossing branch, because a note is easy to write on the path somebody tested.

Three narrower refusals, each a number that would otherwise look reasonable:

- **Extrapolation is capped at the lesser of twice the observed period and twelve months beyond it**
  (Q1E §2.4). The arithmetic will happily return 60 months from six months of data, and the
  confidence band has stopped meaning anything out there. Past the cap the answer is "not within
  what this data supports", which is a statement about reach and not about the attribute.
- **The bound's side follows the drift**, never an argument: bounding a rising impurity from below
  puts the band on the side it is moving away from, and the crossing comes out *later* than the data
  supports — wrong in the optimistic direction, which is the one nobody catches by eye. A flat
  attribute has no drift, so the side comes from the criterion; reading a zero slope as "falling"
  was a real defect and `tests/test_stability.py` holds it.
- **Fewer than three timepoints is refused.** Two fit a line with zero residual degrees of freedom,
  so the band is infinitely narrow and a caller gets their most confident-looking answer from their
  least informative data.
