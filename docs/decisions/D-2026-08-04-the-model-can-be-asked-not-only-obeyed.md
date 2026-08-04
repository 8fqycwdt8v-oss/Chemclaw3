# D-2026-08-04-the-model-can-be-asked-not-only-obeyed — The model can be asked, not only obeyed

**Status:** accepted · **Date:** 2026-08-04 · **Implements:** W5 of
D-2026-08-04-what-bofire-does-when-you-actually-run-it — the last of the five waves

## Context

Every path into the surrogate ended in `ask()`. The model could recommend and nothing could
interrogate it: a chemist could not find out what it expected at a point *they* named, and nobody
could find out whether it predicted anything well. Both are questions people ask before spending a
week of lab time, and both were answered — when they were answered — from the run list, which is
where `op-13` was graded *fabricated*: "is there an unexplored corner, or has the search been
circling one region" is a posterior-uncertainty question, and a list of runs cannot answer it.

## The measurement that reversed a refusal

Cross-validated fit quality was **refused** in an earlier reading of this codebase, on a real
argument: reaching `cross_validate` means naming a surrogate class in `engine.py`, which would
permanently couple us to BoFire's model zoo and — worse — risk reporting a number that describes a
*different* model than the one that made the recommendation.

M-7 measured it and the argument fell: `strategy.surrogate_specs.surrogates` exposes the surrogates
**BoFire itself chose** from the domain (`MixedSingleTaskGPSurrogate` for a mixed domain,
`SingleTaskGPSurrogate` for a featurized one), and `cross_validate` runs straight off them. No class
is named in our code, and the score therefore describes *the* model. `shap` already ships with
`bofire[optimization]`, so nothing here costs a dependency. Ten rows over five folds gave R² 0.948 /
MAE 1.47 in the register, and R² 0.950 / MAE 1.36 driven through our own types — the small
difference is the frame, not a disagreement.

M-6 settled the other half: `predict()` exists after `tell`, accepts a **params-only** frame, works
on a `CategoricalDescriptorInput` domain (the shape a featurized problem actually reaches the engine
as), and does **not** clamp an out-of-bounds point.

## Decision

**One fit serves all three questions.** `_fitted_strategy` builds and fits the strategy the problem
calls for, and propose, predict and cross-validate all go through it. That is not tidiness: it is
what makes the fit quality mean anything. A score measured off a separately configured strategy
would describe a model nobody's recommendation came from.

**`Prediction` is a separate type from `Candidate`, holding the same two numbers.** A candidate is
something the optimizer chose and carries an implicit endorsement — run this next. A prediction
answers a question the chemist asked and endorses nothing. Sharing one type would erase that
distinction exactly where it matters most, in a summary a human reads before booking lab time. The
disclaimer is a `computed_field`, not a docstring, because a bare property is not serialized and the
caveat would never reach the model composing the reply.

**An out-of-domain point is answered and labelled, not refused.** Measured, BoFire extrapolates and
the sd rises sharply — 0.97 in range against 18.6 at T=400 on a 20–120 bound, a nineteenfold rise in
our own types. That rising sd is a better answer than a refusal, *provided* the reader is told which
side of the bound they are on, which is what `in_domain` and the summary's second sentence are for.
`point_in_domain` deliberately ignores constraints: those bound where the *optimizer* may propose,
and a chemist may legitimately ask what the model expects at a point they cannot run.

**The fit quality carries its own denominator.** `folds` and `n_observations` are fields, and below
`bo_fit_quality_trustworthy_observations` the summary says the number is a sanity check rather than
a measure of accuracy. A cross-validated R² over a campaign's worth of runs is the most
over-readable number this module produces: it looks like a statement about the chemistry and is a
statement about ten points.

**Pooled across folds, not averaged over them.** `CvResults.get_metric` returns a `pd.Series`, and
its default `combine_folds=True` computes the metric once over all held-out predictions together.
That is the number reported. A mean of per-fold R² weights a fold of two points the same as a fold
of ten, and at campaign sizes the folds are exactly that uneven.

**One tool, not two.** `predict_outcome` returns the predictions *and* the fit behind them, because
the score is what licenses reading the predictions at all and a model that had to make two calls
would routinely make one. `assess_fit=False` exists for the follow-up question in the same turn,
where the extra fits buy nothing.

**`_require_observed_params_match` became `_require_params_match`.** A prediction goes through
`predict` rather than the acquisition step, so a missing column surfaces as a different library
error — but the caller's mistake and the sentence that repairs it are identical, so the message is
shared. Second real caller, so this is the Rule of Three's second strike, not a speculative
abstraction.

## Consequences

- `op-13`'s remaining half closes. W1 gave it "has this plateaued" from the record; this gives it
  "is there anywhere the search has not looked" from the posterior, and the skill says to answer it
  by predicting at corners rather than by reading the run list.
- Story 3.4 gains a second reading: the sd on a point the chemist chose, not only on one the
  optimizer proposed.
- `predict_outcome` records **nothing**. The points are questions, not proposals, so no campaign row
  is written and no `campaign_id` is returned — the manifest lists it read-only for that reason.
- Feature importance stays unbuilt. `permutation_importance` needs no new dependency, but an
  attribution over four parameters and ten runs is a number this system cannot caveat well enough,
  and it has no second caller. `DEFERRED.md` carries it with its trigger.
- Two config keys, `bo_cv_folds` and `bo_fit_quality_trustworthy_observations`. The second is a
  judgement rather than a derived threshold, and it lives in config precisely so the sentence a
  chemist reads is not a magic number in a docstring.

## One correction carried in from W4

`op-17` was left unrewritten in D-2026-08-04-a-limit-across-parameters-is-not-a-bound, on the
reasoning that it asks for a *coupled* constraint which is still unrepresentable. Re-reading the
probe rather than the summary of it: it asks for **two** limits — "total volume under 5 mL" **and**
"Pd never above 2 mol% whenever the temperature is over 90 °C" — and only the second is conditional.
Its `direction` asserted "the problem specification … has no constraint expression at all", which
W4 made false, and its `forbids_claims` forbade "a constraint expression was added to the problem",
which W4 made the *correct* behaviour for the volume half. So the probe would have graded the right
answer as a failure, which is the same defect this roadmap has now hit in four places — a refusal
outliving its refusal. It is rewritten to grade the split: the volume limit belongs in
`constraints`, the conditional one does not, and treating both alike is the failure in either
direction. Recorded here rather than silently, because an ADR that said a probe was fine was wrong
about it.
