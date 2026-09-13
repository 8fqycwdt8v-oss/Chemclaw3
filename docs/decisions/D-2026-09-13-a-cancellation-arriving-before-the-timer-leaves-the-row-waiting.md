# D-2026-09-13-a-cancellation-arriving-before-the-timer-leaves-the-row-waiting — a cleanup clause covers every await the workflow makes, or it covers the one it is written around

`test_a_wait_started_as_a_child_settles_when_its_parent_dies` established that
`ParentClosePolicy.REQUEST_CANCEL` is the only policy that reaches `AwaitAnswerWorkflow.run`'s
`except asyncio.CancelledError` clause, and its docstring said out loud that it was **not** asserting
the settle actually landed — "driven here it landed on some passes and not others, with a 15 s grace
… that the settle can still be missed is a real gap in the wait, wider than this policy, and it
belongs in its own finding". This is that finding, and the gap is not the one the sentence predicted.

## What was measured

A `BACKLOG.md` row and a scout both read the loss as a **dispatch race**: the settle is scheduled
from a workflow that is already cancelling, so whether the server dispatches the activity before the
run closes is timing. Driven against a real broker, that is false.

With every child *past* its open activity and sitting on `workflow.wait_condition` — the state a wait
spends its entire designed lifetime in — the loss is **zero**:

| children, all on the timer when the parent is terminated | settles lost |
| --- | --- |
| 12 (three runs) | 0, 0, 0 |
| 39 (three runs) | 0, 0, 0 |

The loss is in the windows the `try` did not cover, and there are three of them:

| window | what was observed |
| --- | --- |
| inside `open_pending_request_activity` — the activity that **writes the `waiting` row** — which sat *above* the `try` | 12 parents terminated the instant their children existed: 12 rows opened, **10** settled, every child `CANCELED` |
| inside any activity, because a cancellation there is not `asyncio.CancelledError` | instrumented: `ActivityError(cause=temporalio.exceptions.CancelledError)`, a type the clause did not name. Held deterministically in the open activity: child `CANCELED`, **settled = 0** |
| inside the push-back, which `notify_session_best_effort` caught and carried on from | child **still `RUNNING` 30 s** after its parent was terminated, settle never attempted |

The third is the worst of the three and is a different failure from the other two. A lost settle
leaves a row nobody will move. A swallowed cancellation leaves the wait *alive*: the child goes back
to `wait_condition` on its seven-day timer, so the question stays in every entitled person's inbox,
**answerable**, about work that no longer exists — for up to `awaiting_max_days`. And the helper's own
docstring already drew the distinction it then failed to honour, for a different caller: *"for it,
'delivered' and 'swallowed' are different facts"*.

Every one of the three ends in the same permanent artefact, which that sibling test's docstring
describes exactly: a `pending_requests` row stuck `waiting`, unanswerable because
`POST /pending/{id}/answer` signals a run that is gone and turns the failure into a 503, and never
collected because `pending_requests` is in `retention._NOT_PRUNED`.

## The decision

Three changes, one per window, each necessary on its own (the mutation table below runs each arm):

1. **The `try` opens at the `open` activity, not at the wait.** The row is written by that activity,
   so the cleanup has to cover it. Settling a request that was never opened is harmless by
   construction — `pending_store.settle_request` reports `rowcount == 1`, so a missing row answers
   `False` and writes nothing. That is why this covers the open rather than trying to tell "opened"
   from "not yet opened" inside a cancelled workflow, which is a question the workflow cannot answer:
   the activity's result is precisely what it did not receive.
2. **The clause catches `ActivityError` whose cause is a cancellation, as well as
   `asyncio.CancelledError`.** Which of the two arrives is decided by which `await` was in flight,
   and a cleanup clause that names one of them covers one of them. Everything else an activity can
   raise is re-raised unchanged, which is what keeps a projection refusal a *failure*
   (`test_a_wait_refused_by_the_projection_fails_instead_of_waiting_blind`) rather than a wait that
   quietly reports itself cancelled.
3. **`notify_session_best_effort` re-raises a cancellation as `asyncio.CancelledError`.** It keeps
   its promise about the thing it promised — a failed *delivery* — and stops making one about a
   workflow that is being torn down. This is the general rule and not an `awaiting` special case: a
   cancelled workflow continuing as though nothing happened is wrong at every call site.

**`asyncio.shield` round the settle was considered and is deliberately not added.** It is the fix the
race reading asks for, and the race reading did not reproduce: 0 of 78 settles lost across six runs
once the cancellation arrived where the old clause could see it. A control with no measured input is
the shape this repository keeps finding in its own perimeter, and the three windows above are closed
by naming them rather than by shielding a dispatch that was never observed to fail.

## What it costs

`notify_session_best_effort` can now raise where it could not, and the two callers that run it *on
the way out of an already-failing workflow* both claimed "never raises" as their own contract while
resting on the helper's behaviour:

- `template_job._notify_failure` suppressed `Exception`, and its docstring named "a `cancelled`
  teardown that reaches this line" as a case it covered. `asyncio.CancelledError` has not been an
  `Exception` since 3.8, so that was the one case it did not. Widened to `BaseException`.
- `connector_job._notify_failure` suppressed nothing at all, and the comment at its call site says
  "`_notify_failure` never raises, so a broken push-back cannot replace the real reason with its
  own". That was already untrue — a `ValidationError` building the input would have done it, which is
  the failure `awaiting._push` guards against by hand — and a cancellation would have made it
  reachable. Now suppresses `BaseException`.

In both, the caller's own `raise` re-raises the original failure, so suppressing everything inside
the push-back is exactly what puts the real reason on the wire. Neither change is a new control; both
make an existing stated one true.

`digest.py` reads the return value to decide whether to advance a watermark. A cancellation now stops
it rather than returning `False`; both leave the watermark where it was, and stopping is the better of
the two.

## What is left open, and why it is narrower than it was

A `due_at` reaper — a sweep that settles rows whose deadline has passed and whose run is gone — is a
`BACKLOG.md` row and is **out of scope here**. What it has to cover is now two cases rather than
ordinary operation: a child *terminated* rather than cancelled (which never resumes workflow code at
all — the policy measurement above pins that), and a worker lost between the row write and the
settle. Both are real; neither is reached by a parent dying in the ordinary way, which is what this
closes. A reaper is a new Temporal Schedule with its own retention and `_NOT_PRUNED` argument, and
deciding it on the strength of a defect that has just been fixed would be deciding it for the wrong
reason.

## What keeps it true

| property | test |
| --- | --- |
| a cancellation arriving while the **open** activity is in flight still settles the row | `tests/test_awaiting.py::test_a_cancellation_arriving_before_the_timer_still_settles_the_row` (first arm) |
| a cancellation arriving while the **push-back** activity is in flight still settles the row, and the child does not go back to waiting | same test, second arm — it asserts the settle *and* the `CANCELED` status, because "cancelled but unsettled" and "never cancelled at all" are different failures |
| `REQUEST_CANCEL` is still the only close policy that reaches the cleanup clause at all | `tests/test_awaiting.py::test_a_wait_started_as_a_child_settles_when_its_parent_dies` |
| a projection refusal is still a failure rather than a silent cancellation | `tests/test_awaiting.py::test_a_wait_refused_by_the_projection_fails_instead_of_waiting_blind` |
| the ordinary paths — answer, expiry, escalation, first-answer-wins — are unchanged by the wider `try` | the rest of `tests/test_awaiting.py` |

Both arms are held on an `asyncio.Event` rather than on a sleep, so neither is a race against the
box: the child is *inside* its activity when the terminate lands, by construction, and the activities
are released only after the statuses have been read.

Three mutations were driven, each restored from a `.bak` rather than from git, and each reddens the
new test and nothing else:

| mutation | result |
| --- | --- |
| drop the `ActivityError` arm from the clause | red (1 failed, 12 passed) |
| narrow the `try` back to `_wait_until` alone | red (1 failed, 12 passed) |
| drop the cancellation re-raise from `notify_session_best_effort` | red (1 failed, 12 passed) — and the run logs the swallow it used to make: `session push-back failed for sess-cancel-in-notify: CancelledError` |
