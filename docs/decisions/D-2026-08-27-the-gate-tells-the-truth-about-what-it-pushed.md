# D-2026-08-27-the-gate-tells-the-truth-about-what-it-pushed — the PR-gate's record matches git

## Status

Accepted (2026-08-27).

## Context

The 2026-08-27 knowledge-system review
(`docs/archive/REVIEW-2026-08-27-knowledge-system-analysis.md` §3) traced the PR-gate write path
end to end and found four ways the gate's record diverged from what git actually held, plus one
concurrency model that stopped at the pod boundary:

1. **The lease was self-defeating.** `git_submitter._write_and_push` fetched `note/<id>` into the
   remote-tracking ref one line above `push --force-with-lease`, so the lease was always "whatever
   is on the remote right now" and could never fail — a plain `--force` wearing the safe flag's
   name. A reviewer's fixup commit on a proposal branch was silently discarded by the next
   re-proposal, and `tests/test_knowledge.py` pinned the overwrite as desired.
2. **Every git failure was non-retryable.** `GitSubmitError` covered both the structural refusals
   and the transient failures, and its one name sat in `_BAD_DATA_TYPES` — while
   `note_publish_retry`'s docstring promised that "a genuinely transient `GitSubmitError` (dead
   remote) is retried". A 30-second network blip dropped a note from a synthesis batch on the
   first attempt; `note_write_max_attempts` was dead for exactly the failures it was configured
   for.
3. **The record and the branch could disagree.** The branch is per-note, the record per-version:
   re-proposing a *changed* note left the old version's row `open` (rendering bytes on no
   branch), and the merge webhook's open-rows predicate then marked both rows merged — the
   compliance table asserting a human merged content that was never merged. The no-diff path
   returned a branch reference without a push while `propose_note` still recorded an open row and
   incremented a counter whose own comment says "a note reached the branch".
4. **A dependency file reverted human edits.** Compound notes are re-rendered from SMILES on
   every proposal that links them and were written unconditionally, so a chemist's post-merge
   edit disappeared inside a PR titled as an addition.
5. **The lock was one pod wide.** The `flock` lives in each pod's `emptyDir` clone while
   `service.replicas` autoscales to 6, so concurrent same-id proposals across pods were
   last-writer-wins against one origin with no error — the half of the singleton-worker BACKLOG
   row that made it a correctness problem rather than a scaling one.

## Decision

**A proposal branch may be replaced only while every commit on it is the gate's own.** Every gate
commit carries the `Chemclaw-PR-Gate: submission` trailer, and a submission refuses
(non-retryable) when the remote tip lacks it — a human pushed to the proposal branch, and
resolving that is a decision on the branch, not a retry. The note-branch fetch moves to the start
of the submission, before anything is written, so `--force-with-lease` guards the whole
read-decide-push window; a lease rejection raises the retryable class, and the retry re-enters
through the tip guard, which is what decides whether what landed may be replaced.

**Transient and structural failures get different names.** `GitRemoteError(GitSubmitError)` — a
dead remote, a timed-out command, a contended lock — is the retryable subclass; Temporal matches
`non_retryable_error_types` by exact name, so the subclass's different name is the whole
mechanism. `durable/publish._DECLARED_RETRYABLE` registers the exemption so the completeness walk
in `tests/test_publish.py` still catches an unregistered subclass without forcing every subclass
into the non-retryable list.

**Exactly one open row per note.** A freshly-upserted open version closes the note's previous
open versions with the new `superseded` state (migration `058_note_proposal_superseded.sql`) —
not a decision, not a failure, just "a newer version replaced it in the queue". The merge
webhook's open-rows predicate then has at most one row to move. The migration drops and re-adds
the state CHECK, which the additivity guard flags textually; it does **not** end the
previous-image rollback, because the change widens the constraint and the previous image only
writes the still-allowed states — that reading is this ADR's, and the exemption in
`tests/test_migrations_are_additive.py` cites it.

**`submit` returns what happened.** `NoteSubmitter.submit` returns a `SubmissionOutcome`
(`reference`, `pushed`); the no-diff path returns `pushed=False`, and `propose_note` then records
nothing and counts nothing — the metric keeps meaning what its comment says.

**Dependencies never overwrite.** `NoteFile.overwrite=False` marks the machine-rendered
dependency files; the submitter writes them only where the base branch has none.

**Submissions to one remote serialize across pods** through a Postgres session-level advisory
lock keyed on the remote URL, taken when `session_store="postgres"` — the database every durable
deployment already shares is the one mutual ground, and `pg_advisory_lock` queues rather than
fails, so a waiting pod waits exactly as long as the submission it would otherwise have raced.
The host-local `flock` stays (it guards the worktree sweep, which is per-clone).

## Consequences

- A reviewer may safely commit onto a proposal branch: the gate will refuse to replace their work
  and say why, instead of silently discarding it. The cost is that such a branch must be resolved
  in the git host (merged or deleted) before that note can be re-proposed.
- Transient remote failures now consume retries instead of dropping notes from synthesis batches;
  `fan_out`'s reject-and-continue sees a note only after `note_write_max_attempts` real attempts.
- `GET /proposals` can no longer show two open rows for one note, and `close_merged_notes` can no
  longer mark a never-merged version merged. Existing double-open rows in a deployed table are
  not rewritten by the migration; the next re-proposal of the affected note supersedes them.
- What is *not* taken here: deleting a rejected proposal's remote branch (the decision route has
  no git access by design — the record is the decision), and the read-back tool that would let
  the agent see a rejection's reason. Both remain in the review's findings; the second needs a
  probe-corpus addition and its own sizing.
