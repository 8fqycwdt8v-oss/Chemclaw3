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
  otherwise. `surface_domain_errors` relays that to the *model* for the two job tools — but
  `cancel_job` is not a tool, and neither route had an HTTP handler, so narrowing alone turned a
  wrong-but-quiet 404 into a bare 500 ("this request is broken, do not retry" — the opposite of the
  truth). A `_subsystem_unavailable` handler answers 503 with the error's own chemist-safe message,
  beside the `_database_unavailable` handler that makes the identical argument for Postgres.
- **`start_approval` is unchanged, and that is a reverted fix.** REJECT_DUPLICATE was shipped and
  removed: expiry is not a decision. `InteractionApprovalWorkflow` returns `status="expired"` after
  seven days precisely to drop a candidate rather than pin the workflow, and forbidding id reuse
  makes that candidate unofferable forever while `_announce` still shows a button whose click
  fails — knowledge nobody got round to approving becomes unsavable. ALLOW_DUPLICATE_FAILED_ONLY
  does not help either, because an expired hold *completes*. The distinction no policy expresses is
  "closed with a decision" versus "closed without one", which needs the prior run's terminal outcome
  read before starting — untestable offline here, so it is a BACKLOG row rather than a guess.
- The two MAF discriminators get a named test built from MAF's own public constructors. Mutating
  them already failed seven existing pairing tests, so this closes no gap — it replaces "an
  unanswered call was not stripped" with "the discriminator moved", which sends the next reader to
  the right place. Listing it as a fixed defect was an overclaim.
- A failed job is recorded as `{"status": "failed", "summary": failure_reason(exc.__cause__ or exc)}`
  and reported in the turn — **`__cause__`**, because the client-side `WorkflowFailureError` wraps
  the product's own sentence and passing the wrapper reports the generic "Workflow execution
  failed". `connectors/jobs.py` documents that in a comment; the first version of this change made
  the mistake anyway. The resume preamble now says the jobs *finished* and instructs the model to
  report any failed row, because a sentence asserting completion outranks a status word.
- The note cap is resolved through a local activity, beside `resolve_fan_out_limit`.
- `latest_anchor` walks the newest 16 and returns the first that verifies, logging each skip. This
  raises the attacker's cost from one INSERT to seventeen and **does not close the hole**: past the
  bound it returns None and `verify_chain` still skips its tail comparison silently (measured: 17
  junk rows, `verify_chain` returned `[]`, `make audit-verify` exit 0). The comment claiming the
  control then "reports absent, loudly" was false and is corrected; the real fix belongs in
  `verify_chain` and is a BACKLOG row.

**On skipping versus stopping.** The anchor fix keeps the rotated-key tolerance that motivated the
original behaviour — an invalid anchor is still not a verification failure, because the ordinary
cause is a rotation rather than an attack. What changes is that skipping an invalid anchor no longer
means *stopping* at it. The bound is 16 rather than unbounded, because an attacker who can insert
one row can insert many, and scanning the table to find one valid anchor would trade a silent
failure for a slow one; past that many consecutive invalid anchors the control reports absent,
loudly, which is the honest answer.

**On pinning strings we do not own.** Neither the constants nor the new test are the guard; the
existing pairing tests already were. What the pinning buys is a legible failure. The same argument applies to the five private
`agent_framework` imports the layering review found, which remain open.

## Consequences

An outage now reports as an outage on the two paths where it read as a missing job, which changes
what an operator does about it during a broker roll.

**Inserting that local activity is itself an unguarded history change**, and the repository has no
versioning convention at all (`grep -rn 'workflow.patched|get_version' src/` returns nothing): a
synthesis run in flight across this deploy replays a marker its history lacks and wedges in exactly
the retry loop this fix is about. Drain the three synthesis schedules before deploying it. That gap
is a BACKLOG row.

`_slice_for_this_run` takes its cap as a parameter, so the four tests that set the setting now pass
it explicitly. That is not incidental: the setting is no longer readable from workflow code at all,
which is the property being enforced.

What this does not close is the general case. Four of these six are one shape — reading mutable
state inside workflow code, or reading an upstream library's string as if it were ours — and nothing
enforces either rule. `tests/test_layering.py` cannot see a third-party import at all
(D-2026-08-08 layering findings, still open), and no test asserts that workflow code reads no
`settings` field. Both are enforcement gaps worth closing, and neither is closed here.
