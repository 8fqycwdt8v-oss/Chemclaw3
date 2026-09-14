---
id: solubility-out-of-domain
# A demonstration case: the predicted value is far enough from the reference to exceed
# `eval_prediction_tolerance`, so its failure is the expected result rather than a regression —
# the same idiom `pharma-solvent-heavy` uses for the PMI gate.
expect_pass: false
metrics: [prediction_error]
output:
  predicted: -2.10
  unit: log10(mol/L)
reference:
  actual: -5.20
---
**The demonstration `prediction_error` did not have.** `solubility-benzene` is the passing case:
0.49 log units of error against a 1.0-log-unit tolerance. Nothing else in `data/evals/cases/`
scored this metric, so the tolerance had never once been exceeded in a scored run — and a version
of `prediction_error` that returned `0.0` instead of `|predicted - actual|` would have passed both
`make eval-strict` and `make eval-baseline-check` while measuring nothing.

**The two numbers are constructed, and saying so is the point** — the same posture
`pharma-solvent-heavy` takes with its input masses. A demonstration case's job is to make one
specific gate report a failure; sourcing it from a real measurement would tie the demonstration's
survival to whether that measurement stays wrong, which is the opposite of what a gate wants.
What is real is the *shape*: a 3.1-log-unit miss is what an aqueous-solubility estimator does when
it is asked about a compound outside the congeneric series it was fitted on, and 3 log units is the
error at which a chemist would stop believing the number rather than adjusting for it.

The failure is a property of the pair rather than of the threshold: raise
`eval_prediction_tolerance` past 3.1 and this case stops failing, which
`EvalReport.inert_demonstrations` then reports as lost coverage.
