# D-2026-08-05-a-score-reported-more-precisely-than-it-repeats — A score reported more precisely than it repeats

**Status:** accepted · **Date:** 2026-08-05 · **Amends:**
D-2026-08-04-the-model-can-be-asked-not-only-obeyed (W5's surrogate readback) and the front in
D-2026-08-04-a-trade-off-has-no-single-best-point

## Context

A review of the five shipped BO waves went looking for the gap between what W3 and W5 *claimed* and
what they *do*. It found four, and one of them was a number the tool hands a chemist.

## What was wrong

**1. `predict_outcome` raised on every 3- and 4-run campaign.** `bo_cv_folds` is 5, the tool always
defaulted, and `surrogate_fit_quality` refused when there were fewer runs than folds. So a campaign
above the seeding floor of two but below five — the early campaign a what-if tool is *most* useful
for — got a `ValueError` instead of an answer. Nothing caught it because every test used ten runs.

**2. "One fit" was not what the code did.** The ADR, the tool docstring and the PR body all said the
prediction and the score come from one fit, and that claim is the stated reason the score describes
the model that made the prediction. The code called `_fitted_strategy` twice.

**3. The fit-quality numbers were reported far more precisely than they reproduce.** This is the
finding that matters. BoFire fits the GP's hyperparameters by numerical optimization and **that fit
is not deterministic** — not under a pinned `torch` seed, and not with a fresh `deepcopy` of the
surrogate specification (both measured, both still drift). Twelve identical calls on one ten-run
problem:

| | min | max | spread | sd |
| --- | --- | --- | --- | --- |
| R² | 0.906 | 0.969 | **0.063** | 0.020 |
| MAE | 1.16 | 1.80 | **0.639** | 0.213 |

MAE varied by **more than half its own value**. It was printed to three significant figures and R²
to three decimals. A chemist comparing "R² 0.950" against "R² 0.927" from two calls would be reading
noise as a difference between two models.

**4. The Pareto front ignored the assay noise a plateau verdict requires.** W1 made `assay_noise`
required because a difference inside the assay is not a difference; W3's front then treated a 1e-12
difference as real and dropped a run from the trade-off a chemist should choose along.

## Decision

**A defaulted fold count adapts to the data; a stated one does not.** `_resolve_folds` caps a
defaulted `bo_cv_folds` at the run count (floor 2) and still refuses a fold count the *caller* named
that the runs cannot carry. `FitQuality.folds` records what was used, so the adaptation is visible.
The distinction is the same one the screening knobs draw: the system's own default may bend to the
data; a number the caller stated is a claim, and silently changing it answers a different question.

**`interrogate_surrogate` is the single entry point, so "one fit" is arithmetic rather than
rhetoric.** `predict_at` and `surrogate_fit_quality` become thin wrappers over it. This turned out
to matter more than the wording: because the hyperparameter fit is non-deterministic, fitting twice
produced genuinely *different* models, so the old claim was false rather than merely imprecise.

**The score is printed to the precision it survives a repeat at** — R² to two decimals, MAE to two
significant figures — and its summary says so in words: re-running gives a different number, and a
small difference between two scores is not a difference between two models. The alternative was to
make the fit deterministic, which is not reachable from here: the seed and the spec copy were both
tried and neither holds it still.

**`pareto_front` gains a `tolerance`, defaulting to exact.** A per-objective difference of
`tolerance` or less is no difference in either direction, so two runs the assay cannot separate
never dominate one another. `suggest_next_experiment` gains an **optional** `assay_noise` forwarded
into it — the same name and meaning as `campaign_progress`'s required argument, so the two tools
cannot drift into two definitions of "a real difference".

It stays optional here, unlike W1, and the difference is real: a front without the number is still a
true statement about the runs as recorded, whereas a plateau verdict without it is not. The honesty
comes from saying which was computed — `ExperimentSuggestion.front_tolerance` carries it and the
summary states either "runs differing by X or less were treated as indistinguishable" or "no assay
reproducibility was given, so every numeric difference counted as real".

**An absent fit says so.** `SurrogateAnswer.summary` returned `""` when `assess_fit=False`. A blank
caveat reads as "no caveat", which is the opposite of what it means.

## One of the review's own numbers is retracted

The review that prompted this ADR reported `predict_outcome` at "8.6s against 28.7s", and called the
default a tripling of latency. **That comparison was not controlled** — it was the first and second
call in a cold process, so the first carried the warm-up. Measured properly afterwards, the
duplicate fit this ADR removes costs **0.61s** (mean of five, 0.59–0.63), and a warm
`predict_outcome` over ten runs with the fit assessed is **4.5s**, against 0.5s without. The
structural facts stand — there was a redundant fit, and it is gone — but the speedup implied by
those two numbers was mostly process warm-up.

Recorded rather than quietly corrected, because it is the third time in this task that a number
travelled further than the measurement behind it, and `tasks/lessons.md` R5.5 exists for exactly
this. A one-shot timing in a cold process is not a benchmark.

## Consequences

- `predict_outcome` answers a 3-run campaign in about 1.8s at 3 folds, where it previously raised.
- `tests/test_bo_predict.py` pins the reproducibility as a **property** — repeats land in a band and
  the summary warns against reading small differences — rather than pinning a value that does not
  exist. The prediction *is* deterministic (`strategy.predict` on a fitted strategy is arithmetic),
  so that half is still asserted exactly.
- A second test asserts the *formatting*: two decimals on R², two significant figures on MAE. Prose
  drifts; a regex over the summary does not.
- No campaign id, persisted row or existing front moves: `tolerance` defaults to `0.0` and is
  pinned by a test that the default reproduces the old front exactly.
