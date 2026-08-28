# D-2026-08-27-an-interval-is-only-honest-where-it-was-calibrated — an interval is only honest where it was calibrated

**Status:** accepted · **Date:** 2026-08-27

Closes the `BACKLOG.md` row "Split-conformal uncertainty is unwired" by **declining** it, and
states the trigger that would reopen it.

## The row, and what it asked for

The row proposed re-adding `science/calc/uncertainty.conformal_uncertainty` — deleted for having no
caller — and wiring it into `predict_solubility`, which already calls `_log_prediction` and is one
`await reconciled_for(...)` away from the ledger. It framed the open question as a *policy* one:
"which predictors are calibrated enough to override their published RMSE", to be settled by
returning `calibration_conformal_coverage` and `calibration_conformal_min_samples` as config.

The wiring is genuinely ~100 lines and the row is right that it is not a cross-repo decision. It is
declined anyway, on three measurements. Each was run rather than argued.

## 1. There is no calibration set, and nothing is on a path to producing one

Against the live Postgres (`make up`, migrations applied):

```
SELECT calc_type, calc_version, count(*), count(observed_value) FROM predictions GROUP BY 1,2;
 (0 rows)
SELECT count(*) FROM predictions;    -> 0
SELECT count(*) FROM measurements;   -> 0
```

Zero is not a property of this checkout. It is what the write path produces:

- `calibration_enabled` defaults to **False** (`core/config/memory.py`), so a shipped deployment
  records no prediction at all.
- A residual needs `predictions.observed_value`, which is written only by
  `calibration.record_observation`. That function has **exactly one caller in the whole tree** —
  the `report_measurement` MCP tool, `source="chemist-reported"`. No ELN adapter, no ORD ingest, no
  Temporal Schedule, no bulk importer writes `measurements`; `grep` for the INSERT finds the one
  statement in `calibration.py` and the one `CREATE TABLE` in `infra/sql/030_measurements.sql`.
- The match is on `stable_hash(canonical_smiles)` — the *same molecule*, not a similar one. A
  chemist must have typed a measured value for the exact compound the calculator predicted.

So `n` is bounded by bench work someone chose to transcribe, one molecule at a time. The ceiling
available without new bench work is the repository's own committed reference data, and that is
**one value**: `data/evals/cases/solubility-benzene.md`, log S = −1.64 (Delaney 2004). It is not
wired to `measurements` and would be n = 1 if it were. `data/vendored/` holds 36 reagent *names*,
no experimental properties.

`reconciled_for` is the read that would feed the interval. Today it returns `[]` on every
deployment, and would keep doing so until a human has typed in enough measurements.

## 2. At every feasible `n`, the estimator is noise

Split conformal takes the `ceil((n+1)·coverage)`-th smallest absolute residual. At the row's own
proposed values — coverage 0.9, min samples 20 — that is the **19th of 20**: an extreme order
statistic estimated from one sample of it. Simulated over 20 000 trials with residuals drawn
N(0, σ) and σ = the published RMSE (so the true 90% half-width is 1.645σ):

| n | rank used | median | p05 | p95 | P(off by >25%) |
| --- | --- | --- | --- | --- | --- |
| 9 | 9 of 9 (the maximum) | 1.792 | 1.079 | 2.782 | 42.0% |
| 20 | 19 of 20 | 1.738 | 1.236 | 2.372 | **23.8%** |
| 50 | 46 of 50 | 1.684 | 1.364 | 2.052 | 5.8% |
| 200 | 181 of 200 | 1.653 | 1.488 | 1.829 | 0.0% |

The first `n` at which 90% of runs land within ±20% of the true half-width is **n = 59**. At n = 20
roughly one deployment in four would publish a half-width off by more than a quarter, and the 5th
percentile is a 25% *under*-estimate — the direction that reaches a chemist as an over-tight bar.
At n = 9, the smallest n for which a 90% interval exists at all (`ceil((n+1)·0.9) ≤ n`), the
"interval" is literally the largest residual ever seen.

## 3. The override the row exists to enable cannot fire, because the two numbers are not the same quantity

`predictions.predicted_uncertainty`, `SolubilityResult.uncertainty_log` and `Estimate.uncertainty`
all carry **one standard deviation** — `SolubilityResult`'s docstring says so, and
`Calibration.uncertainty_coverage` is defined as the fraction of observations falling inside ±1σ. A
90% conformal half-width is a different quantity in the same field. Two consequences, both measured:

**It never narrows.** With a perfectly honest published RMSE, P(90% half-width < 1σ) at n = 200 is
0.0%. The override fires only if the calculator is much better in this deployment than published:

| σ_deployment / σ_published | P(conformal 90% < published 1σ) |
| --- | --- |
| 1.00 | 0.0% |
| 0.80 | 0.0% |
| 0.70 | 1.2% |
| 0.61 | 44.8% |
| 0.50 | 99.9% |

For ESOL's shipped 0.75 log units that means the model would have to be running at ≈0.46 log RMSE
here before the interval could narrow. If that were ever true, the correct fix is to correct the
published constant, not to attach a second interval beside it.

**At matching semantics it is a number we already report.** Targeted at 1σ (coverage 0.6827), the
conformal half-width converges on the sample RMSE `Calibration.rmse` already computes — median
absolute difference 0.127 log units at n = 20, **0.032 at n = 200**, far inside the 1.0-log-unit
tolerance the solubility eval case uses. And `calculator_trust` already surfaces that RMSE, already
gates it behind `calibration_min_observations`, and already carries a `verdict` distinguishing a
disabled ledger from an empty one from too few points. Re-adding the function would be a second
answer to a question the ledger answers, differing from it by less than its own noise.

Mixing the two would also break the one figure that could tell an operator the wiring worked:
`uncertainty_coverage` scores stored uncertainties against ±1σ, so a stored 90% half-width would
read as ~90% coverage and be indistinguishable from an over-wide, well-behaved 1σ.

## Decision

**Do not re-add `conformal_uncertainty`, and do not restore `calibration_conformal_coverage` /
`calibration_conformal_min_samples`.** The ledger, its residuals and `calculator_trust` stand
unchanged; they are the honest form of this capability at the sample counts that exist.

The stale claim in `core/config/calculators.py` that "the *function* stays, tested and correct" is
corrected in the same commit — it is false, and a comment asserting a deleted artifact still exists
is the shape this repository keeps having to delete.

`tests/test_uncertainty.py::test_no_conformal_interval_is_re_added_without_a_reader` is the guard.
It does not forbid the re-add; it fails an **unwired** one, in either half — a
`conformal_uncertainty` with no caller outside its own module, or a `calibration_conformal_*`
setting with no reader in `src/`. That is the same absence shape as
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`.

## The trigger that reopens this

All three, measured, not asserted:

1. **`n ≥ 59` reconciled residuals for one `(calc_type, calc_version)`** —
   `SELECT calc_type, calc_version, count(*) FROM predictions WHERE observed_value IS NOT NULL
   GROUP BY 1,2`. 59 is the simulated point at which a 90% split-conformal half-width lands within
   ±20% of the truth 90% of the time; anything smaller publishes the sampling noise of an extreme
   order statistic as a confidence interval.
2. **A producer of measurements other than a chemist typing one in.** At one measurement per
   transcribed bench result, (1) is years away; a bulk experimental corpus or an ELN property
   adapter is what changes the arithmetic, and building the estimator before the producer is
   building it for a corpus that never arrives.
3. **A stated resolution of the σ mismatch.** Either the interval is reported at 1σ — in which case
   say why it is not `Calibration.rmse`, which it agrees with to 0.032 log units — or
   `predicted_uncertainty` grows a coverage level and `uncertainty_coverage` is redefined against
   it. Emitting a 90% half-width into a 1σ field is not one of the options.

Until then, the honest interval for a solubility prediction is the published RMSE with the
calculator's measured bias quoted beside it, which is what `calculator_trust` returns today.

## Alternatives considered

- **Build it and gate on `n ≥ 20`, as the row proposed.** Rejected on §2: 23.8% of deployments
  would publish a half-width off by more than 25%, and an over-tight solubility bar reaching a
  chemist is the harm the row itself names.
- **Build it, refusing to narrow below the published RMSE.** This is the shape the task brief
  sketched, and it is coherent — but §3 measures that under an honest published constant the
  refusal fires essentially always, so the mechanism reduces to "report the published RMSE": the
  ~100 lines would be a no-op with a `Method` member attached to it.
- **Report the empirical residual quantile instead, at 1σ.** That is `Calibration.rmse`. It exists,
  is exposed, and is gated. Nothing to add.
