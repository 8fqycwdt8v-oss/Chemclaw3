# D-2026-09-14-an-activity-name-is-a-wire-name-so-it-is-renamed-in-two-releases — `propose_report` becomes `record_report_note`, with the old name still registered

## Status

Accepted. First of two releases; the second is a deletion, and its trigger is in `DEFERRED.md`.

## Context

`durable/report_workflow.py::propose_report` calls `record_note`. There is nothing to propose to:
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` removed the gate and the proposal queue
behind it, and wave 15 corrected every *docstring* on that path. It could not correct this, because
the name is not prose — it is the string a Temporal history schedules against.

A symbol name is read far more often than the docstring under it, and this one names a control that
does not exist. But renaming it in one commit is not safe: an in-flight `DevelopmentReportWorkflow`
history that has scheduled `propose_report` and not yet completed it resolves against whichever
worker picks the task up next. A worker that no longer offers the name fails the activity with
`NotFoundError`, and the workflow retries it — forever, since no redeploy will ever bring the name
back.

## Decision — what the name now means, and the release procedure

**`record_report_note` is the activity**: it renders a gathered report as a `report` note in the
knowledge graph and returns the reference. No gate, no queue, no review — the note is written.
`DevelopmentReportWorkflow` schedules this name.

**`propose_report` is a compatibility alias and nothing else.** It is registered under the old
Temporal name via `@activity.defn(name="propose_report")`, takes the identical signature, and
delegates. Two properties are deliberate:

- *The signature is identical, `correlation_id` included* — a parameter no body reads.
  `durable/interceptor.py` binds an activity's ids by **parameter name** off the signature, so an
  alias that dropped it would make a replayed old task the one unattributed write on this path.
- *It delegates rather than duplicating.* Two functions writing a note is two chances for them to
  disagree about what a `report` note is.

**The release procedure, which is the part that is not a commit:**

1. **This release.** Both names registered on `background-jobs`; the workflow schedules
   `record_report_note`. A history scheduled before the deploy still resolves.
2. **Drain.** No `DevelopmentReportWorkflow` execution started before step 1 is still open. That is
   observable rather than assumed: `temporal workflow list --query 'WorkflowType =
   "DevelopmentReportWorkflow" AND ExecutionStatus = "Running" AND StartTime < <deploy time>'`
   must return nothing. The upper bound on the wait is a report workflow's own run timeout.
3. **Next release.** Delete `propose_report`. One function, its test import, and the alias entry in
   the worker's activity list.

Step 3 is a `DEFERRED.md` row with step 2 as its trigger, not a `BACKLOG.md` row: it is not work
somebody can start, it is work a condition unblocks.

**Merged ADRs that cite the old name are not edited** — `D-2026-08-27-…` among them. A merged ADR is
never edited, so the citation outlives the rename either way, which is why this ADR says in as many
words what the name now names.

## Consequences

Two activity names on one queue until step 3, which costs one extra entry in the worker's registry
and nothing at runtime — a name nothing schedules is never dispatched.

## What keeps it true

- `tests/test_report_workflow.py::test_the_old_activity_name_is_still_registered_and_still_writes`
  — both names are offered by a `background-jobs` worker, and the alias's signature matches the
  activity's. Mutation: changing the alias's signature fails it with the interceptor's reason.
  Deleting the alias outright fails the module's import instead of that assertion — blunter, but
  loud, and the worker's activity list needs the symbol either way.
- `tests/test_report_workflow.py`'s end-to-end workflow runs — they drive a real Temporal worker
  registered with both names, so the workflow scheduling the *new* one is executed rather than
  asserted.
