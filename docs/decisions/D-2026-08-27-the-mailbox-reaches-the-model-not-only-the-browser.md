# D-2026-08-27-the-mailbox-reaches-the-model-not-only-the-browser — job push-back joins the next turn's input

## Status

Accepted. Closes the agent-facing half of the job→session push-back (F3) that shipped with only a
browser-facing consumer; `mid_turn_resume_enabled` keeps its default and its ADR
(`D-2026-08-12-a-held-permit-is-the-price-of-a-mid-turn-resume`) unchanged.

## Context

A durable job that outlives its turn writes a `session_events` row, and `claim_unconsumed` had
exactly one consumer in `src/`: the SSE push-back stream a browser tab holds open. With the tab
closed, the completion reached nobody — and either way it never reached the *model*, whose next
turn began not knowing work it had launched was done. The system's defining interaction —
"compute this, then reason about the result" — therefore required the chemist to re-prompt and
the model to remember the job id well enough to poll `get_durable_job_status`, one full
conversation turn per poll.

D-2026-08-12 already weighed the *mid-turn* answer and kept it off by default, on a measured cost:
a resume holds an admission permit for up to 60 s against a p50 turn of 8.1 s. Its own text names
the alternative this ADR builds — "the job→session push-back mailbox delivers for free."

## Decision

The runner reads the mailbox at turn start (`_with_pushed_job_results`): waiting `job_completed` /
`job_failed` rows are claimed with the same atomic, kind-scoped claim the SSE stream uses, and
appended to the chemist's message as framed data — chemist's words first, workflow output framed
by `frame_untrusted` because a job summary is data, never an instruction, with the failure
instruction stated the way the mid-turn resume's message states it. The claim's atomicity is what
arbitrates the two consumers: a live tab's tailer and the next turn cannot both deliver one row,
whichever asks first wins, and both audiences are told the same way. Best-effort in both
directions — a mailbox that cannot be read must not fail the turn, and a memory-backed deployment
has no mailbox to read.

Beside it, two delivery holes in the channel itself are narrowed:

- **The at-most-once window shrinks to the transport.** The tailer's claim used to be destructive
  across the whole claim-commit-to-SSE-write gap; a drop in it silently destroyed the only signal
  that a chemist's long search had finished. `stream_new_events` now restores a row whose yield
  never completed (`restore_unconsumed`, on a teardown-safe task), trading a possible duplicated
  "job finished" card for a silently lost one — the cheap side.
- **A dropped push-back is counted.** `notify_session_best_effort` swallows failures by design
  (the science is the result; the notification is not), which made a fleet-wide outage of the
  channel invisible. `chemclaw_pushback_dropped_total` (replay-guarded) is the aggregate an
  operator can alert on; `chemclaw_rejoin_describe_failed_total` does the same for the rejoin
  announcement's one quiet failure.

The model's own poll is also made worth a turn: `get_durable_job_status` long-polls
`handle.result()` for `job_status_wait_seconds` (Temporal's own long-poll, never a sleep loop)
before answering `running`, so a poll landing moments before completion returns the result now
instead of spending another full turn setup to learn it. The HTTP job route deliberately does not
wait — a browser's poll is cheap and holding its request open is not — which is why the wait is a
parameter of `job_status` rather than a fork of it.

## Consequences

- `mid_turn_resume_enabled` stays `False` with its measured rationale intact; the mailbox path is
  the permit-free half of the same interaction and works with the resume on or off.
- The unconsumed-rows retention question (rows for sessions nobody reopens accumulate, since only
  consumed rows are pruned) stays open in `docs/planning/DEFERRED.md` with its own trigger — the
  next turn now consumes most of them, which shrinks the population the question is about.
