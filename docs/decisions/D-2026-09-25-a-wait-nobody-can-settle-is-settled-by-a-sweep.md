# D-2026-09-25-a-wait-nobody-can-settle-is-settled-by-a-sweep — a collector for orphaned durable waits

**Status:** accepted · **Date:** 2026-09-25 · Closes the `BACKLOG.md` row *"A `pending_requests`
row whose run was terminated, or lost with its worker, has no collector"*, left open by
`D-2026-09-13-a-cancellation-arriving-before-the-timer-leaves-the-row-waiting`.

## Context

`pending_requests` is a projection of `AwaitAnswerWorkflow`, and only the run moves its row out of
`waiting`. A run that is **terminated** never resumes workflow code — the default
`ParentClosePolicy.TERMINATE`, or an operator acting on the wait directly — and a run that fails or
times out, or whose settle is cancelled after it closed, leaves the row the same way. Such a row sits
in every entitled person's inbox and cannot be answered: `POST /pending/{id}/answer` signals a run
that is gone and returns 503. `pending_requests` is in `retention._NOT_PRUNED` on purpose, so
nothing else collects it.

## Decision

**An hourly Temporal Schedule, `orphaned-waits`, settles a waiting row `cancelled` when the broker
says the run that owns it is not running** (`durable/orphaned_waits.py`).

- **The broker decides, not the clock.** A row is settled only on `describe` answering a status
  other than `RUNNING`, or `NOT_FOUND` (a history retention removed), or `INVALID_ARGUMENT` (a run id
  no run could own — raising there would stop every later row on every pass). `due_at` is not the
  test: a running wait past its deadline is its own timer's business, with the notice that goes with
  it. Any other broker error stops the sweep, because an unreachable broker would otherwise read as
  "gone" and cancel live questions.
- **A lost worker is not an orphan.** Its run is still `RUNNING` and resumes on the next worker.
- **Whichever run of the id is running owns the question.** An operator's reset — the documented
  remedy for a nondeterminism failure on exactly this workflow — terminates the run the row names
  and continues the wait in a new one that replays past the activity which wrote `run_id`. So a
  row whose own run is closed is also checked against the workflow's latest run, and left alone if
  that one is running. Found by review before merge; the test's `reset` case is red without it.
- **Guarded on the run it examined** (`pending_store.settle_orphan`): `_OPEN` reopens a request id
  under a new run and rewrites `run_id`, so evidence about a dead run never settles a live one.
- **Settled, never deleted**, with the reason in `answer`; the table stays out of retention.
- **Unconditional**, unlike its neighbours: no setting turns waits on, so none should turn their
  collector off. With no waits a pass is one empty indexed query. Rows younger than
  `awaiting_orphan_grace_seconds` (300) are not asked about; `awaiting_orphan_batch` (200) bounds a
  pass.

Driven against a real broker and table (`tests/test_orphaned_waits.py`): a terminated run's row and
an unknown run's row are settled with their reasons; a running wait's row and a reopened row are
untouched. Red with the `RUNNING` check removed (the live question is cancelled) and red with the
run guard removed (the reopened question is cancelled).

## Alternatives

- **A `due_at` reaper.** What the row proposed. Declined as the test: it races the run's own
  expiry, and it cannot see a terminated wait before its deadline, which for a 90-day ask is the
  whole of its life in the inbox.
- **`ParentClosePolicy.REQUEST_CANCEL` everywhere.** Already the policy at every call site
  (`test_every_wait_started_as_a_child_names_a_parent_close_policy`), and it does not reach an
  operator's terminate or a failed run.
- **What stalls it.** A broker error other than `NOT_FOUND`/`INVALID_ARGUMENT` on the oldest row
  (a permission refusal, say) stops every pass at that row, so no later row is reached until it is
  fixed. Deliberate — the alternative settles on an unreadable answer — and visible only as the
  activity's failure in the Schedule's `last_outcome`.
- **A race with the detached cancel settle.** A run cancelled after the grace window can be seen
  `CANCELED` before its own detached settle lands; both write `cancelled`, so the cost is the
  sweep's reason text replacing the run's.
- **Notify the requester on an orphan settle.** Declined for now: the run that knew who to tell is
  gone, and the row's `requested_by` is advisory. **Revisit when:** a chemist asks what happened to
  a question that vanished from an inbox — visible as `awaiting.orphan_settled` in the background
  worker's log on a row with a non-empty `session_id`.

## What keeps it true

- `tests/test_orphaned_waits.py::test_a_terminated_wait_is_settled_and_a_live_one_is_left_alone`
- `tests/test_schedules.py::test_plan_covers_all_periodic_jobs`
- `tests/test_workflow_registry.py::test_every_background_workflow_holds_the_stance_argued_for_it`
