# D-2026-10-03-a-turn-is-written-ahead-and-an-interrupted-one-says-so — write-ahead and honest interruption, not durable turns

**Status:** accepted · **Date:** 2026-10-03 · **Builds on**
`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop` (a response is a view of a pump),
`D-2026-10-01-a-queued-message-waits-in-its-senders-request` (the claim a queued turn takes),
`D-121` (the per-session turn claim, a lease), `D-2026-08-10` §2 (the transcript is a read model).
**Amends** `D-2026-08-10` §2's "written once, after the answer".

## Context

Measured on the kind cluster (K5 §1, two front-door replicas, a scripted slow turn): force-deleting
the pod serving a turn 6 s in

- cut the client's stream with no terminal event (unavoidable — the process is gone);
- made the reattach (`GET /sessions/{id}/turn/stream`) on the surviving replica answer
  `404 no turn is running` — true, and useless: a turn that finished, one on another replica and one
  that will never answer all say it;
- left the chemist's message **out of the transcript** (`GET /messages` → `[]`) and **in the
  LangGraph checkpoint**, so the next turn's model read a question the chemist could not see;
- booked **no `turn_costs` row**: the outcome vocabulary is written in the turn's own teardown, and a
  SIGKILL runs none.

Nothing ran twice and a retry worked at once. The cause is by design: `session_messages` was written
once, by `_record_transcript`, after the answer existed, while the checkpointer commits the question
at the graph's first step. A turn dying in between leaves the two records disagreeing and nothing
marking the turn.

## Decision

### 1. The question is written ahead of the turn

`run_turn` writes the chemist's message to `session_messages` before the graph runs
(`_begin_transcript_turn` → `begin_turn`), stamped with the turn's correlation id and sender as every
row is, and with a new column `turn_status = 'running'` (migration 118, additive, nullable, a partial
index over the running rows). When the turn ends, its own process settles that row in the same
commit that appends the rest of the exchange (`finish_turn`):

| ending | status | what is appended |
|---|---|---|
| answered | `done` | the tool exchanges and the answer, exactly as before |
| raised, empty answer, wall-clock timeout | `failed` | nothing |
| stopped (cancelled, not by the clock) | `stopped` | nothing — settled from a task, since a cancelled teardown may not `await` (D-130) |

The transcript's shape for its readers is unchanged: the question is the same `HumanMessage` row in
the same position, followed by the same rows, read by the same `message_from_row`; the status rides
in `additional_kwargs` the way the correlation id does, and reaches the wire as an optional, additive
`TranscriptMessage.turn_status`. A provider without `begin_turn` (the CLI, a test's recorder), or a
write-ahead the store refused, falls back to writing the exchange whole, as before.

**An answer overrides; a non-answer never demotes.** `done` is written unconditionally — including
over `interrupted`, which another process may write when a live owner missed its refreshes (the
lease property D-121 states) — because the answer the chemist received is the record. Every other
status settles only a row still `running`, so a teardown racing its own successful write, or
arriving after another process's mark, cannot undo a settled turn.

### 2. A turn whose owner died is marked `interrupted` by whoever notices, exactly once

A question is interrupted when it is still `running` and **no live claim covers it**: the session's
`session_turns` row is absent, expired, or was claimed *after* the question was written
(`claimed_at <= created_at` fails — only a successor's claim, taken once the dead turn's lease lapsed,
does that; a refresh moves `expires_at` and never `claimed_at`). One `UPDATE … RETURNING` makes it
exactly-once across processes: the row lock serialises concurrent noticers and only the statement
that flipped the row gets it back.

Who notices: the session's **next turn** (before its own write-ahead, holding the claim), a
**reattach** that finds nothing running, and a **transcript read**. The noticer books the turn's
outcome — one `turn_costs` row with `outcome='interrupted'` and zero spend (what the dead process
metered died with it), one `turn.interrupted` log record, one
`chemclaw_turns_finished_total{outcome="interrupted"}`. `interrupted` is kept out of `_OUTCOMES`,
whose one producer is `_settle_outcome`; it is `runner.INTERRUPTED`, with its own producer
(`settle_interrupted_turns`).

### 3. The reattach says so

`GET /sessions/{id}/turn/stream` answers **410** `{"code": "turn_interrupted", "message": …}` when
nothing is running here and the session's newest written-ahead turn is `interrupted`. Otherwise it
answers as before (200 to follow, 404 for nothing running). A client cut off mid-stream can then say
"this answer was interrupted (the service restarted)" and offer to send the question again, instead
of polling the transcript for an answer that will never come.

### 4. The checkpoint is left alone, and the two records now agree about what was asked

The interrupted question is in the checkpoint (the graph committed it) and in the transcript (marked
`interrupted`), so the chemist sees what the model will see on the next turn. Nothing is rolled back
from the checkpoint: an interrupted turn may have left a tool call there, and deleting committed
graph state from a process that did not write it is the riskier act. A session fork copies a
`running` question as `interrupted`, since nothing will ever settle the copy.

## Alternatives considered

- **(b) Reattach consults the checkpointer and answers 410 with the last checkpoint step; (c) the
  next turn detects a checkpoint whose last human message has no transcript row** (the K5 report's
  other two options). Each answers *one* reader, needs the checkpoint's internal shape at a route,
  and leaves the transcript missing the question for every other reader; the write-ahead makes the
  transcript itself the record of what was asked, and all three readers ask one predicate.
- **Project the transcript from the checkpoint stream** (`_record_transcript`'s own declined
  alternative). Closes the whole divergence class, including the answer half of a teardown after the
  graph run (`chemclaw_transcript_thread_divergence_total`); costs a projection over LangGraph's
  internal state on every turn. Not needed to close the defect measured here.
- **A periodic sweeper.** A session nobody touches again keeps its question `running` and its outcome
  unbooked until someone does — the next turn, a reattach or a reload. Accepted: the ledger row is
  for a turn someone might ask about, and the first ask books it. A sweep would need a Temporal
  schedule over every session for a row nobody is reading.
- **Durable turns: run the turn itself in Temporal**, so a dead front door's turn resumes on another
  worker. Declined: it moves layer 1 into layer 2 (graph steps as activities, the stream as a
  workflow query, the connector sessions a turn holds re-opened per activity), which
  `D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use` already found to be two durability
  layers for one conversation; and the measured cost of the defect is one lost turn that the chemist
  retries at once with nothing run twice — honesty about the loss closes it, resumption is a much
  larger promise.

Revisit when: a measured rate of mid-turn front-door loss makes retries a real cost — the
`chemclaw_turns_finished_total{outcome="interrupted"}` series against `chemclaw_turns_started_total`
on a production dashboard is the number that would show it — or the front door drops multi-replica
session affinity (`deploy/helm/chemclaw/values.yaml` serving replicas, `D-121`), so a turn's pump
can no longer be assumed to live in one process for its whole run.

## Consequences

- `session_messages` now holds a question for a turn still running; a transcript read during a turn
  shows it `running` (a participant of a shared session sees the question before the answer).
- A failed or stopped turn's question is now in the transcript, marked, where before it was absent
  from the transcript and present in the checkpoint.
- The transcript route and the reattach route can write (the mark); both are one indexed probe when
  nothing is running.
- Wire: `TranscriptMessage.turn_status` (optional) and the reattach's 410 `turn_interrupted` are
  additive. `Chemclaw3_ui` reads both; its PR is compatible with a service that sends neither and
  merges first.
