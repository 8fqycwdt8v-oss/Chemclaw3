# D-2026-09-04-a-review-of-a-review-finds-the-fixes — six fresh contexts over the merge that fifteen fresh contexts produced

**Status:** accepted · **Date:** 2026-09-04 · Reviews the merge that
`D-2026-09-04-fifteen-fresh-contexts-over-one-tree` records, and **corrects that ADR's central
claim**. Supersedes nothing; the decisions it reviewed stand, and the code implementing several of
them did not.

## Context

The hardening pass merged 192 files of fixes, each with a test, under a green `make ci`. Six
reviewers were then given the merged diff, no account of why it was written, and one instruction:
find what is wrong with it.

They found sixteen defects **in the fixes**, six of them HIGH. That is the number worth recording,
because every one of those fixes had been reproduced before it was written, tested with a failing
test first, and passed a full gate.

## What the fixes got wrong, by kind

**A fix that reintroduced the class it was closing.** `_pg_dial` was written to stop a DSN's TLS
posture being guessed at, and interpolated libpq's parse error into its refusal — libpq quotes the
offending token, which for a typo'd scheme is the whole DSN. The credential reached stderr during
`import chemclaw.core.config`, *before* `configure_logging()` installs the redacting filter. The
docstring one line above had already promised the opposite.

**A bound that stopped bounding past the width its test swept.** The per-result ceiling divided by
the batch width was correct until the share fell under the notice explaining the cut, at which
point every result floored there and the total grew linearly: 124,800 characters at width 400
against a 60,000 ceiling. The test parametrised widths 8 and 20; the crossover is at 188.

**A fix applied one level too low.** Framing the two free-text columns of `job_records` was right
for the model and wrong in `job_status`, which is also the whole body of `GET /jobs/{id}` — so
envelope markup reached an HTTP client while `GET /jobs` returned the same row raw. The comment
saying the envelope belongs to the agent layer *because* the front door reads that function was
read, and then contradicted.

**Two clocks where the fix needed one.** The commitment sweep marked with
`activity.info().started_time` (the Temporal server) and compared against `observed_at` (Postgres).
Measured: a quarter-second skew makes every pass delete the mirror it just wrote, permanently,
while reporting success.

**A catch that swallowed cancellation.** Narrowing `except Exception` to `except ActivityError` so
one failing source could not end a sync was right in intent; workflow cancellation arrives as
exactly that type, so a cancelled drain ran to COMPLETED.

**And a chart fix that broke the other direction.** Moving two objects from Helm hooks to tracked
resources fixed a rollback that restored pods and left the old configuration — and made `helm
rollback` *delete* both while restoring Deployments that mount them, and `helm upgrade` from the
previous chart fail at prepare time on unadoptable objects.

## The correction owed

`D-2026-09-04-fifteen-fresh-contexts-over-one-tree` argues that five HIGH chart defects survived
earlier reviews because `helm` is absent from the sandbox, twelve chart assertions skip, and
installing helm was therefore the pass's highest-yield act. **The first half is true of the sandbox
and the conclusion is wrong about CI.** `azure/setup-helm` is pinned only in the `chart` job, but
the `check` job runs on `ubuntu-latest`, which ships Helm — so the chart tests have been running in
CI throughout.

The defects survived because the tests **rendered the default values and nothing else**. That is a
different and less flattering lesson than the one recorded: not a gate that could not run, but a
gate that ran against one configuration. The merged ADR is not edited, per this repository's rule;
this is the correction.

## Decision

**A fix is a change, and a change gets reviewed by somebody who did not write it.** A green gate
over a diff whose author also wrote its tests is evidence about internal consistency and not about
correctness — sixteen defects passed one here.

Three properties of the *tests* were the recurring cause, and are the part worth carrying:

1. **A test that substitutes its own copy of the thing under test proves nothing about the thing.**
   The clock test forced both clocks equal; the readiness test called `broker_seen_recently()`
   rather than the worker's `ready()`. Deleting half of the production predicate left 75 tests
   green.
2. **A parametrised test is evidence only over the range it sweeps.** Widths 8 and 20 said nothing
   about 400, and the property genuinely fails there.
3. **A guard with no test is a guard that will be deleted.** "An empty answer never sweeps" had
   none; removing it wiped a mirror with fifteen tests green.

## Consequences

`sweep_withdrawn`'s mark is now read from the mirror's own database, which constrains what a future
caller may pass it. The readiness predicate moved out of a closure to module scope *so that it could
be tested* — a design changed by testability rather than decorated with a test.

**Left open, deliberately**: a connector manifest can declare `awaits_answer` and thereby remove the
operator's wall-clock ceiling on that job, gated by nothing, in a tree whose bundle directory is
operator-extensible and which disables stdio transport by default because "a manifest is data".
Both available fixes overturn the argument in `child_execution_timeout` that no finite number is
right, so the gate is a decision rather than a repair, and it is not taken here.
