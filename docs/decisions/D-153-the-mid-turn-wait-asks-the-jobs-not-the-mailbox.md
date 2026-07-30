# D-153 — The mid-turn wait asks the jobs, not the mailbox

**Status:** accepted · **Context:** REV-7. The review item was "a push-back notification lost
between claim-commit and delivery is lost permanently". Verifying it turned up a second, worse
defect in the same area — one that *destroys* notifications rather than losing them to a crash —
and that is what this ADR fixes. The original item stays open, with the shape it actually needs.

## The defect: a wait that consumed other people's mail

`chemclaw.agent.job_results.await_job_results` — the mid-turn resume (AGT-2) — tailed the push-back
mailbox to learn when its jobs finished. The mailbox claim is **destructive by design**: a claim
marks every unconsumed `job_completed` row for the session as consumed and returns them in one
atomic step (COR-4). So a resume waiting on job A also consumed the row for job B — a job that turn
did not start — kept only what it was waiting for, and **dropped the rest on the floor**. The front
door's `/sessions/{id}/events` stream, the consumer those rows belong to, never saw them: the
chemist is simply never told job B finished.

The old docstring half-knew this and argued it away:

> a non-matching one would already have been claimed by the front door's own
> `/sessions/{id}/events` stream, which is the consumer that owns them

That is a race, not a guarantee. Both consumers poll the same rows on their own timers; whichever
claims first wins, and when the resume wins, the event is destroyed.

Dormant only because `mid_turn_resume_enabled` is off by default — which is exactly when it is
cheapest to fix, and the same argument that motivated D-145 and D-146.

## Decision

**Wait on the jobs, not on the mailbox.** Each job id is awaited on its own Temporal workflow
handle; the mailbox is not touched at all.

"Did job X finish" is a question about durable state, and durable state is the authoritative answer.
The mailbox exists to *wake a chat* — a different job, with a different consumer, and a queue whose
semantics (destructive, at-most-once, session-scoped) were never meant to serve a targeted "is this
specific job done" query. Using it for that was the category error underneath the defect.

Consequences that fall out of asking the right source:

- **There is no shared queue left to race over.** The resume consumes nothing, so it cannot destroy
  anything, and the front door's stream is unaffected whether the resume runs or not.
- **`handle.result()` is Temporal's own "tell me when this finishes"**, so there is no poll interval
  to tune and no mailbox latency between a job completing and the wait noticing.
- **The model resumes with the result, not a description of it.** A `job_completed` payload carries
  a one-line `summary`; the `ConnectorJobResult` envelope carries `data`. The resume was handing the
  model the summary and calling it the result.
- The waits are gathered with `return_exceptions=True`, so one failed or undecodable job does not
  cancel the others — the turn resumes with whatever landed. A *failed* job is reported with its
  status rather than omitted: "your calculation failed" is an answer the chemist needs inside this
  turn, and dropping it would leave the model narrating a success that did not happen.

`completed_job_status` is extracted from `get_durable_job_status` and shared by both. That module's
docstring claims a finished job's result is collected in exactly one place — D-118 made that true
and a second decode here would have quietly ended it.

## What this deliberately does not fix

**The original REV-7 — a notification lost between claim-commit and delivery — is still open**, and
the plan that first accompanied this change was wrong about it. "Select, yield, then confirm" does
not work: `stream_new_events` polls on a timer and has no `try`/`finally`, so an event yielded but
not yet confirmed is re-selected on every subsequent poll.
`test_tailer_releases_its_connection_between_polls` makes it concrete — 40 polls over ~2 s asserting
one delivery would see ~37. Preventing re-selection of an in-flight event *is* a visibility timeout;
there is no cheaper version.

The real fix therefore remains what `BACKLOG.md` already recorded: claim with a lease and a delivery
deadline, confirm on delivery, re-offer on expiry — preserving COR-4's single-holder property while
making loss recoverable. It needs a migration, a **per-stream** holder id (`_WORKER_ID` is
per-process, so two streams in one pod would steal each other's leases), a confirm step shielded
against cancellation (D-130's exact trap, since the confirm is reached from a cancelled generator),
and `event_id` in the SSE payload. It is also an operator-facing contract change —
`docs/archive/audit/11-handover.md` lists at-most-once delivery as one. Its own ADR.

**The `eval_drift` channel is left alone.** It looked like a third defect: `eval_drift` events are
written by `chemclaw.durable.eval_drift` and claimed by nothing, so they accumulate. But the channel
constant's own comment says it is "a `session_events` 'session' **an operator surface tails**" — the
consumer is *unbuilt*, not missing by accident. That is a backlog item, not a bug, and deleting the
write would discard a signal someone deliberately started emitting.
