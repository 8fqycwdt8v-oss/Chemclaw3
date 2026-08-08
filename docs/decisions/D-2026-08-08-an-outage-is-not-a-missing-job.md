# D-2026-08-08-an-outage-is-not-a-missing-job — six durable failures that reported the wrong thing

**Status:** accepted

## Context

Six defects in the durable layer, sharing a shape: **a failure was reported as a different, more
benign fact.**

**1. Every Temporal `RPCError` was reported as "no such job".** `job_status` and `cancel_job` caught
the bare exception and never read `.status`, so UNAVAILABLE, DEADLINE_EXCEEDED, RESOURCE_EXHAUSTED
and PERMISSION_DENIED all produced `ValueError: no durable job with id …` and an HTTP 404. The
inline rationale — "Temporal has never heard of this id, which for a job that genuinely ran means
its history has aged out" — is correct for NOT_FOUND and for nothing else. An operator cancelling a
runaway DFT run during a broker roll was told the run did not exist, stopped trying, and the cluster
kept burning.

**2. A decided approval hold reopened as pending.** `start_approval` is one of five copies of the
launch idiom and the only one that passed no `id_reuse_policy`. temporalio defaults to
ALLOW_DUPLICATE, so `WorkflowAlreadyStartedError` fires only while the prior run is *open*: once a
hold had been approved, rejected or expired, re-surfacing the same candidate started a fresh run
under the same id. A human clicks No, the surface re-renders the candidate, and a second click can
flip a recorded GxP sign-off — under a docstring promising "the hold already exists — idempotent
surface".

**3. Two MAF strings decided which rows a nightly sweep deletes.** `durable/retention.py` asks
`message_pairing` which chat rows are safe to drop, and the answer turns on
`content.type == "function_call"` / `"function_result"`. Those are MAF's strings. Measured against a
plausible rename, `droppable_rows([(1, call), (2, result)], {1})` went from `set()` — the partner
correctly protected — to `{1}`, deleting the call and stranding its result, which that module calls
"a bricked session with *no* self-heal path". No exception, no failed activity: sessions stop
working days later.

**4. Mid-turn resume dropped failed jobs.** `asyncio.gather(..., return_exceptions=True)` collected
the failures into a return value that was never bound, so a job whose workflow failed was simply
absent from `collected`, the runner's `if results:` skipped the resume, and the model finished the
turn on its pre-wait text — narrating a success that did not happen. The only trace was an INFO line
reading "no result yet", which asserts the job is still pending when it has failed permanently. The
module's own docstring promised the opposite.

**5. A live settings read decided how many children a synthesis starts.** `_slice_for_this_run`
returns the input list to `fan_out`, so `memory_max_notes_per_run` decides how many
`StartChildWorkflow` commands a workflow task emits. Measured with `workflow.now()` pinned and the
corpus fixed: `cap=25` emitted 25 children, `cap=10` emitted 10. A redeploy that lowers the value
mid-fan-out replays 10 starts against 25 recorded child-started events — a non-determinism error,
which is a workflow *task* failure, which retries forever ignoring the retry policy and wedges the
run. `orchestrator.py` states this rule and captures its own bound through a local activity; the
line above it in the same function did not.

**6. One junk anchor disabled tail-truncation detection.** `latest_anchor` read exactly one row and
returned None if its signature failed, so appending a single unsigned anchor — needing INSERT, not
the key — made `audit_chain` set `held_to = None` and skip its comparison, and a trail truncated to
any length verified clean. Strictly less work than the tampering the anchors are said to catch.

## Decision

- Both `RPCError` handlers narrow to `RPCStatusCode.NOT_FOUND` and raise `SubsystemUnavailableError`
  otherwise, which `surface_domain_errors` already relays to the chemist as an outage.
- `start_approval` passes **REJECT_DUPLICATE** — not the `ALLOW_DUPLICATE_FAILED_ONLY` the four job
  launchers use, because the cases differ: a failed *job* should be re-runnable, while a decided
  *hold* is a GxP record and re-opening it is the defect whatever it decided.
- The two MAF discriminators are pinned by a test built from MAF's own public constructors, so a
  rename fails on the day of the upgrade rather than weeks later in a corpus.
- A failed job is recorded as `{"status": "failed", "summary": failure_reason(exc)}` and reported in
  the turn.
- The note cap is resolved through a local activity, beside `resolve_fan_out_limit`.
- `latest_anchor` walks the newest 16 and returns the first that verifies, logging each skip.

**On skipping versus stopping.** The anchor fix keeps the rotated-key tolerance that motivated the
original behaviour — an invalid anchor is still not a verification failure, because the ordinary
cause is a rotation rather than an attack. What changes is that skipping an invalid anchor no longer
means *stopping* at it. The bound is 16 rather than unbounded, because an attacker who can insert
one row can insert many, and scanning the table to find one valid anchor would trade a silent
failure for a slow one; past that many consecutive invalid anchors the control reports absent,
loudly, which is the honest answer.

**On pinning strings we do not own.** The constants are not the guard — the test is. Pinning them in
one place only gives the test somewhere to look. The same argument applies to the five private
`agent_framework` imports the layering review found, which remain open.

## Consequences

An outage now reports as an outage on the two paths where it read as a missing job, which changes
what an operator does about it during a broker roll.

`_slice_for_this_run` takes its cap as a parameter, so the four tests that set the setting now pass
it explicitly. That is not incidental: the setting is no longer readable from workflow code at all,
which is the property being enforced.

What this does not close is the general case. Four of these six are one shape — reading mutable
state inside workflow code, or reading an upstream library's string as if it were ours — and nothing
enforces either rule. `tests/test_layering.py` cannot see a third-party import at all
(D-2026-08-08 layering findings, still open), and no test asserts that workflow code reads no
`settings` field. Both are enforcement gaps worth closing, and neither is closed here.
