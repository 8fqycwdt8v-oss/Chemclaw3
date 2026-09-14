# D-2026-09-14-a-gate-nothing-has-failed-is-a-gate-that-cannot-fail — a gated metric no case has ever failed is a gate that cannot fire

**Status**: accepted
**Date**: 2026-09-14

## Context

`make eval-strict` and `make eval-baseline-check` both run in `make ci` and are labelled there as
"the scientific quality gates". `BACKLOG.md` recorded the complaint against them: *the two eval
gates score literals written in their own case files* — 11 of 13 baseline metrics are arithmetic
over numbers committed in `data/evals/cases/`, so "a metric that stops measuring and answers
'perfect' passes both".

That complaint was correct and its stated mechanism was only half the story. `EvalCase.expect_pass`
and `EvalReport.inert_demonstrations` already exist for exactly this hazard, and they work: a
threshold loosened until a demonstration case stops firing turns `--strict` red. What they cannot
see is the case one level up. `inert_demonstrations` asks, *per case*, whether a case declared
`expect_pass: false` still fails something. A **metric** with no demonstration case anywhere is
invisible to it — there is no case to go inert.

Measured on the shipped case-set at `940f8510`, six metrics are gated
(`e_factor`, `pmi`, `plan_quality`, `prediction_error`, `retrieval_recall`, `runaway_rate`) and
**two of them had no case anywhere in `data/evals/cases/` that made them report a failure**:
`runaway_rate` and `prediction_error`. Both had been green since the day they were written. Neither
had ever been observed to fire.

Both mutations were run to confirm that is what it sounds like:

| Mutation | `make eval-strict` before | after |
|---|---|---|
| `prediction_error` returns `value = 0.0` instead of `abs(predicted - actual)` | **exit 0** | exit 1 |
| `runaway_rate` returns `value = 0.0` instead of `len(runaways) / len(raw)` | **exit 0** | exit 1 |

A metric replaced by a constant "perfect" passed the gate that exists to catch exactly that, and
put its constant into `baseline.json` where the drift check compared it against itself.

## Decision

**A gated metric owes the case-set one case that makes it fail**, the same way a fix owes the suite
a test that goes red without it. `EvalReport.gates_no_demonstration_can_fire` reports the gated
metrics that no `expect_pass: false` case actually fails, `--strict` exits non-zero on a non-empty
list, and `render_report` names them — because, as with `inert_demonstrations`, nothing appears in
a failure table to point at a gate that was never exercised.

Two demonstration cases close the gap for the two metrics that were missing one:
`autonomy-runaway-cap-fires` (a capped turn beside a clean one, rate 0.5 against a 0.0 limit) and
`solubility-out-of-domain` (a 3.1-log-unit miss against a 1.0-log-unit tolerance).

**An ungated metric owes nothing.** `turn_cost_ratio`, `bo_regret` and the three set metrics report
a number rather than a verdict; there is no threshold to demonstrate, and demanding a failing case
for them would be demanding a failure of something that cannot fail.

## Consequences

- The case-set version moved to `gates-2026-09-14` and `baseline.json` was refreshed. Two
  aggregates moved because a demonstration case joins the mean, exactly as `pharma-solvent-heavy`
  already does for `e_factor`/`pmi`: `runaway_rate` 0.0 → 0.25, `prediction_error` 0.49 → 1.79.
  Neither is a regression and neither is a target.
- Adding a **new gated** metric now costs a demonstration case as well as a passing one. That is
  the intended price: the alternative is the two gates this ADR found.
- **What this does not fix, stated so it is not read as fixed.** The 11 pinned metrics still score
  literals, so they still cannot see a *system* regression — only a broken metric or an edited
  case. `turn_cost_ratio` is the one where that gap is the whole point of the metric, and it is
  treated separately (see the live-ledger work in the same wave). `evals.baseline.render_comparison`
  already labels which metrics are live and which are pinned; this ADR does not change that count.

## What keeps it true

- `tests/test_evals.py::test_every_gated_metric_has_a_case_that_makes_it_fail` — over the shipped
  case-set, with a positive control that drops the demonstrations and requires every gated metric
  to be named, so the assertion is not satisfied by the subject returning an empty list.
- `tests/test_evals.py::test_strict_mode_fails_when_a_gated_metric_stops_measuring` — substitutes a
  constant-"perfect" metric into the registry and requires exit 1.
- `tests/test_evals.py::test_an_ungated_metric_owes_the_set_no_demonstration` — the other
  direction, so the check cannot be satisfied by gating nothing.
