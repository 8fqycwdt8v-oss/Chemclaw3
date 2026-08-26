# The envelope is not the result: make the composite publish path route (PR 1)

## Task
`D-2026-08-25-a-cache-is-not-a-record` claims every computed value reaches an external
results store. Measured on `main`, 10 of 11 durable jobs and 2 of 10 primitive calculators
publish nothing. Fix the routing, and replace the guard that could not see it.

## Measured before changing anything
- `projector_for("calc.compute_reaction_energy", "XtbJobResult")` -> `None`; production
  `enqueue_payload` queues **0** rows, the same call with the inner model queues **1**.
- `projector_for("developability", "")` -> `None` (the table says `descriptors`; the calc
  server and `tests/calc_server_fake.py` both emit `developability`).
- `tests/test_publish_reaches_the_hooks.py::_SHIPPED_JOBS` hardcodes four pairs: one names a
  tool that is not a job, and the two calc ones pair a route with a shape production never
  builds. The calc bundle ships nine jobs.

## Plan
- [x] `XtbJobResult.outcome()` — the one populated member, derived, not a field list
- [x] `CalcJobWorkflow.run` publishes the member, not the wrapper (matches `qm`, the one
      bundle that works). Forced by layering: `publish -> connectors` is not an allowed edge
- [x] `_CALC_TYPE_PROJECTORS`: `descriptors` -> `developability`; delete the three prefixes
      nothing emits (`xtb.thermo`, `xtb.scan`, `xtb.energy`)
- [x] `_SHIPPED_JOBS` derived from the manifests + `XtbJobResult`'s own member fields, with
      the four unprojectable GFN models declared in `_NOT_YET_PUBLISHED` (PR 2 empties it)
- [x] `tests/test_step_handoff.py` + `data/templates/conformer-refinement.yaml` follow the
      flattened `data`
- [x] The five stale-prose edits from the review
- [x] `make lint type test` green; re-run the measurement and show it inverted

## Review
The elegant version was not the first one, and the test forced it.

**The fix I first wrote was three lines inside `CalcJobWorkflow.run`, and the test I wrote for it
copied those three lines.** That is the same error one level down: a copy agrees with whatever it
was copied from at the moment it was written, and the whole reason this seam shipped inert is that
`_SHIPPED_JOBS` was a hand-written statement about code it never called. So `job_envelope` is a
module-level function, `run` is one line, and the test calls the function rather than re-deriving
its body. Verified the guard bites by reverting the fix in place: it fails with
`assert 'XtbJobResult' == 'ReactionEnergyResult'`, which is exactly the defect.

**Two things measurement changed my mind about.** I had planned to delete the four dead prefixes
in `_CALC_TYPE_PROJECTORS`. Checking each against `XtbTask` and every engine's `CALC_TYPE` before
and after the physics move showed `xtb.scan` *was* a real task pre-move, and
`calculation_results` is never pruned — so deleting it would have made the backfill skip real
historical rows. It stays; the three that never existed (`descriptors`, `logd`, `xtb.thermo`,
`xtb.energy`) go. And I checked the migration runner before editing the comments in `051`/`053`:
the drift checksum is over statements only, confirmed empirically by re-running `make db-migrate`
against the already-migrated dev database (`already up to date`).

**What is deliberately not fixed**, each with a BACKLOG row rather than a silent omission: the
four multi-step GFN shapes (PR 2 — declared in `_NOT_YET_PUBLISHED`, so a tenth member field still
fails), `xtb.hess` and `ThermochemistryResult` (needs a third hook, not a projector), and the BO
campaign question (write the projector or say it deliberately does not publish).

## Result, measured the same way as before

| | before | after |
| --- | --- | --- |
| primitive calculators publishing | 8 of 10 | 9 of 10 (`xtb.hess` declared) |
| durable jobs publishing | 1 of 11 | 6 of 11 (4 declared, 1 undecided) |

`make lint type test`: 4699 passed, 3 skipped (the migration-history checks needing
`fetch-depth: 0`), lint and `mypy --strict` clean. Five validators re-run green. End to end against
Postgres, a reaction-energy job and a descriptor panel each queue 1 row where both queued 0.
