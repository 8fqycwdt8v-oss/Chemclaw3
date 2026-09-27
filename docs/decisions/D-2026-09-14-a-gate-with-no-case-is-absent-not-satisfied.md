# D-2026-09-14-a-gate-with-no-case-is-absent-not-satisfied — where the unfireable-gate check reads its gates from

**Status**: accepted. Closes a hole in
`D-2026-09-14-a-gate-nothing-has-failed-is-a-gate-that-cannot-fail`.

## Context

`EvalReport.gates_no_demonstration_can_fire()` exists because a gated metric scored only over
cases written to pass is a gate that has never been observed to fail — and a version of it that
stopped measuring and answered "perfect" would move nothing. It was added with the measurement that
two of six gated metrics were in exactly that position.

## The finding

It derived its `gated` set from **this run's results**: `{r.result_metric for r in self.results if
r.passed is not None}`. A metric that no case scores produces no rows, so it is not in that set, so
it cannot be reported as unfireable. **The one arrangement the check exists to catch is the one
that removes its input.**

Driven on the shipped case-set — both `runaway_rate` cases moved out of `data/evals/cases/`:

- `make eval-strict` → **exit 0**.
- `make eval-baseline-check` → exit 2, *"Worsened: runaway_rate (0.25 → absent, lower is better)"*.

So one control did see it. But a case-set change bumps `EVAL_CASE_SET_VERSION`, and the documented
response to that is to **re-record the baseline** — which erases the only control that caught it.
The two together are a path where a gate leaves the set and every command goes green.

## Decision

**Gatedness is declared at registration and the check reads the registry.** `@metric(name,
direction, gated=True)` follows the shape `live=True` already has, for the same reason: it is a
property of the metric, and the only other place to read it from is a run's own output. Six
metrics carry it — `e_factor`, `pmi`, `prediction_error`, `plan_quality`, `runaway_rate`,
`retrieval_recall`.

**The flag is reconciled rather than trusted.** A declaration nothing checks is the failure this
repository keeps finding in its own prose, so every metric the shipped case-set actually scores is
checked in both directions: one that returns a verdict must be declared `gated=True`, and one that
never does must not be. A metric no case scores is outside what a run can say — which is precisely
why the declaration has to exist.

After the change, the same deletion gives `make eval-strict` **exit 1**: *"1 gated metric(s) have
no demonstration case: runaway_rate"*.

## Consequences

- A new gated metric owes the case-set a failing case *and* the registration flag, and forgetting
  either is loud: no flag makes the metric invisible to the check again, and a flag with no case
  fails `eval-strict` until the case exists.
- Deleting the last case of a gated metric now fails CI rather than quietly reducing coverage.

## What keeps it true

- `tests/test_evals.py::test_a_gated_metric_whose_cases_are_all_gone_is_still_owed_a_demonstration`
  — the deletion, on the shipped case-set, asserting that neither of the other two `--strict`
  clauses is what produces the finding.
- `::test_every_scored_metrics_gatedness_is_the_one_it_declares` — the flag against the verdicts,
  both directions.
- `::test_every_gated_metric_has_a_case_that_makes_it_fail` and
  `::test_strict_mode_fails_when_a_gated_metric_stops_measuring` — unchanged, and still the
  positive controls for the check itself.
