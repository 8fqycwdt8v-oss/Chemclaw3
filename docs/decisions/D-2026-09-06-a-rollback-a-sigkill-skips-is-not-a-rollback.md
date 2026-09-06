# D-2026-09-06-a-rollback-a-sigkill-skips-is-not-a-rollback — the note checkout recovers from a hard kill, the push reaches its classifier, and a cached result is offered before it is persisted

## Status

Accepted (2026-09-06).

## Context

Two wave-6 reviewers reached the same defect from opposite directions — one driving hard kills
through the durable tier, one driving delivery failures through `kg/git_writer.py` — which is why
it is one decision rather than two.

**Everything `GitNoteWriter` does to keep a write all-or-nothing runs in an `except BaseException`,
and a `SIGKILL` runs no handler.** A pod eviction, an OOMKill or a node drain past the grace period
between the `git add` and the `git commit` leaves the note's blobs staged and its bytes in the tree
with nothing anywhere to clear them. Measured: with the remote unchanged that residue is harmless —
the next write fast-forwards and its path-limited commit ignores it — but once another pod pushes
the same note paths, `merge --ff-only` refuses, `_replay_our_unpushed_commits` finds no commits of
ours to replay and refuses too, and **every later note write on that pod fails forever**: three
unrelated writes, all `GitRemoteError`, index still staged after each. That is the two-pod case
`_cluster_lock` exists for, not a corner. Nothing recovers it: `knowledge-sync.sh::refresh_note_repo`
fast-forwards with the same `--ff-only` and treats a divergence as a warning by design.

The same kill leaves a second residue. `_exec` kills its child with `SIGKILL` on a timeout and on
cancellation, and measured, the child's `.git/index.lock` survives — while `_exec`'s own docstring
claimed in the present tense that it "can never … orphan a git child holding `.git/index.lock`".
Every `git add` in the checkout then failed, and it failed **non-retryably**: a local command's
failure is `GitWriteError`, which is in `durable/publish._BAD_DATA_TYPES`, so `note_publish_retry`
spent no attempt on it and `publish_note_best_effort` swallowed the first. `note_write_max_attempts`
did nothing for the one git failure that clears itself.

Two further findings on the same file, both the shape `D-2026-08-26-an-attribution-nothing-can-write-
is-not-an-attribution` named. **The push never reached its auth classifier:** `_is_auth_failure` is
called only from `_git(transient=True)`, whose one call site is the *fetch*, while `_push` called
`_run` directly and raised `GitRemoteError` unconditionally — so all five forge denial wordings a
2026-08-28 survey added, every one of them push-side, were unreachable. Driven against a
`pre-receive` hook emitting each: four came back **retryable** while `_is_auth_failure(stderr)`
answered `True` about the same bytes. A read-only token is the shape that hurts — the fetch
succeeds, so the classifier's one live call site never sees it. Its two tests call the function
directly, which is what let it look alive. **And a note whose push failed is live in the local
graph** — `_write_and_commit` lands the bytes in the tree `load_notes` scans and busts the cache
before pushing, deliberately — while the caller was told only "git push to main failed". That is the
message `surface_domain_errors` shows the model, and `GitWriteError`'s own docstring records what
the model does when told a note write failed: five retries permuting its arguments, then the
document printed into the chat.

Separately, `cached_compute` persisted and *then* published: `store.put` at `store.py:555`,
`publish_stored_result` at `:561`. A hit returns above both, so D-011 — a persisted result is never
recomputed — is exactly what makes a crash between them permanent. Measured with a kill in the gap:
`computes=1 publishes=[]`, no counter moved, indistinguishable from a calculation nobody ran, and
`publish backfill` is the only recovery with **no Schedule running it**.

Last, `fan_out`: a child without `@workflow.defn(failure_exception_types=[...])` does not fail, it
*parks* in the SDK's task-failure loop, and over three probe children (ok / raise / hang) the
raising one and the hanging one produced the identical "Child Workflow execution timed out" line
after the full `fan_out_child_timeout_seconds`. The contract is in `fan_out`'s own docstring;
`tests/test_workflow_registry.py` enforces it over the *job path* registry, which does not see a
third `fan_out` caller.

## Decision

**A recovery for a state defined by a handler not running belongs where the flock is held, and the
discard belongs where git has already said the checkout cannot move.**

1. `_clear_a_stale_index_lock` runs at the top of every write, under the flock. Two conditions, each
   covering what the other cannot: the flock proves no *peer writer* is mid-write, and an age bound
   (`git_command_timeout_seconds`, the ceiling every git child of this module is held to) covers the
   one holder the flock does not exclude — a person running git in the clone by hand. A younger lock
   raises the **retryable** `GitRemoteError` instead (`_is_a_contended_index`, the one local failure
   classified transient), and the retry is what reaches the sweep once the lock has aged.
2. `_discard_a_dead_writes_residue` runs **between the failed fast-forward and the rebase**, not on
   every write. A staged note in this clone is indistinguishable from residue — that is what a kill
   leaves — so nothing can separate "recover" from "discard an operator's work" except whether the
   checkout still moves. While it does, nothing is discarded, which is what
   `test_poisoned_index_does_not_leak_into_the_next_write` has always asserted and is right to.
   Once git says it cannot move, the alternative to discarding is that the pod records no knowledge
   again, ever. Scoped to `knowledge_dir` — every path this writer can stage — and logged at WARNING
   with the paths. **Un-staging alone is not enough and this was measured**: the residue is then
   *untracked*, and `--ff-only` refuses an untracked overwrite exactly as it refuses a staged one.
3. `_push` goes through `_git(..., transient=True)`, so a denial is classified and git's stderr
   reaches the `git.failed` record; the raise is re-wrapped **keeping the class `_git` chose** and
   adding what the caller cannot see — the note is committed on the base branch and readable here,
   only the push did not happen, re-record nothing.
4. `cached_compute` **offers before it persists**. The outbox row carries the projected payload, not
   a reference into `calculation_results`, and `enqueue_payload` never raises, so nothing depends on
   the row existing first. Reversed, the same kill leaves a queued row and no cache row: the next
   call recomputes once and re-enqueues onto `ON CONFLICT … DO NOTHING`. That trades an undetectable
   permanent gap for a bounded, self-healing recompute, and makes "persisted implies offered" —
   which `publish_stored_result`'s docstring already claimed — true of the cache rather than
   intended. **D-011 is untouched**: nothing is recomputed that was ever persisted.
5. `fan_out` refuses a child whose `__temporal_workflow_definition` declares no
   `failure_exception_types`, at the one seam that knows it is a fan-out child. A child carrying no
   definition at all is left alone: it is a stand-in for the SDK, not a workflow.

## Consequences

- A pod wedged by a hard kill recovers on its next write instead of dropping every note until a
  human intervenes; the two residues are named in the log lines an operator will now see.
- **A staged knowledge-dir path can be discarded** — only in a checkout git has already refused to
  fast-forward, only inside `knowledge_dir`, and only at WARNING with its paths. This is the
  accepted cost: a person who staged a note by hand in a wedged clone is the one holder this cannot
  tell from a corpse, and the alternative is a permanent wedge.
- A denied credential now fails a note write **non-retryably**, which is the point: no number of
  retries installs a token. A transient rejection keeps the retryable class, driven in the test
  beside it so the fix cannot degrade to "call everything auth".
- A cache miss whose process dies costs one recompute. That is the first deliberate exception to
  "never recompute" being *paid* rather than avoided, and it is paid only on a crash.
- Three stale claims were corrected in place because their own diffs falsify them: `_exec`'s
  "can never … orphan a git child holding `.git/index.lock`"; the rollback comment's "the next
  write's `merge --ff-only` refuses because of it" (measured, only when the incoming commits touch
  the staged paths — the two-pod case); and `publish_stored_result`'s "the calculation succeeded and
  is already persisted", which the new order makes false at the one call site that must be right
  about it.

## Deliberately not done

- **No new metric.** `chemclaw_notes_recorded_total` is fed by `WriteOutcome.written`, which `_push`
  and `test_a_push_that_failed_is_pushed_by_the_next_attempt_of_the_same_note` define as "the remote
  gained this note" — so a note stranded by a failed push is counted when the retry pushes it, and
  the counter under-counts only notes that never reach the remote, which is what
  `chemclaw_notes_publish_failures_total` counts. Making it count the *local* landing needs a second
  field and re-opens that decision; the divergence gauge that would make an unpushed backlog visible
  (`rev-list --count <remote>/<base>..HEAD`) is a `core/metrics.py` series. Both are backlog, not
  this ADR.
- **No Schedule for `publish backfill`.** Point 4 closes the gap at its cause, so the backfill stays
  the operator's tool it is.
