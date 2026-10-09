# D-2026-10-09-a-turn-whose-pod-died-resumes-until-it-has-acted — its sender's next attach continues it from the checkpoint, unless it made a state-changing call

**Status:** accepted · **Date:** 2026-10-09 · **Builds on**
`D-2026-10-03-a-turn-is-written-ahead-and-an-interrupted-one-says-so` (the honest ending this record
keeps for every turn it will not continue), `D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica`
and `D-2026-10-08-limits-and-bookkeeping-live-in-postgres` (the leases and the awaited bookkeeping it
relies on). **Revisits** the durable-turns decline of `D-2026-10-03`, for the one case its own
trigger named: a turn whose process is lost between two steps.

## Context

Programme item W3.6. Measured with two real front-door processes on one database, the real compiled
graph (Postgres checkpointer, the full middleware chain) and a model that is a function of the
thread, one process SIGKILLed at a named point (`tests/replica_graph.py`):

- **The client** sees its stream cut with no terminal event.
- **The other replica** answers `GET …/turn/stream` with 503 while the claim's lease runs (the
  holder does not answer) and, once it lapses, **410 `turn_interrupted`**; the question is
  `interrupted` in the transcript and one `turn_costs` row says `interrupted` with zero spend.
- **The transcript is consistent** (the dead question is there, marked); the next
  `POST …/messages` runs normally and the model reads the dead question again. Nothing ran twice.
- **The checkpoint holds a resumable state.** Killed between two model calls: `next = model`, the
  thread is `[question, call, result]`. Killed inside a tool: `next = tools`, the thread ends on the
  unanswered call. `astream(None, config)` continues both. It also runs the tool body again: a
  state-changing tool whose result was never recorded ran twice, once cut off and once resumed.

So the system is already honest about a lost turn; what it cannot do is finish it, though everything
needed to is in the database. The cost is the chemist's wait for the turn to be asked again, and for
the long ones (a minute of calculations) the work.

## Options

1. **Keep ending it `interrupted`.** Nothing to build, nothing repeated. Every lost turn is lost.
2. **Resume it from the checkpoint on its sender's next attach, unless it has acted.** The turn is
   continued by `GET …/turn/stream` of the person who sent it, as that person, once, if the thread
   shows no state-changing call.
3. **Resume anything from the checkpoint**, repeating an in-flight state-changing call. A defence
   would need every state-changing tool to be idempotent, which a tool's author cannot promise (a
   job launch, a note commit, a preference write) and the audit row of the cut-off call died with the
   pod. Declined.
4. **A sweeper resumes dead turns without a client.** The turn would need an identity to run as
   without a person attached: the roles are in the sender's token and a stored copy would outlive
   its revocation. Declined, with a trigger.
5. **Run the turn in Temporal** (`D-2026-10-03`). Moves layer 1 into layer 2. Still declined.

## Decision

**Option 2.** A turn that dies between two steps is not marked at once: it stays `running` for its
sender. The rule, and where each part lives:

- **Dead** means the `D-2026-10-03` predicate: still `running`, no live claim covering it. The claim
  that covers a question is the one taken at or before `COALESCE(resumed_at, created_at)`
  (`session_store.COVERED`).
- **Eligible** (`LapsedTurn.eligible`): never resumed (`session_messages.resumed_at IS NULL`), younger
  than `service_turn_timeout_seconds` (a turn that old would have timed out), and no outcome booked
  for its correlation id. Migration 126 adds `resumed_at` and `dry_run`.
- **Judged** (`turn_resume.judge`) on the checkpointed thread: the question is found by its message id
  (`q:<correlation id>`, which `run_turn` now gives the chemist's message), and after it there are
  only the model's tool calls and their results, the last assistant message possibly still waiting for
  results. **No call, finished or in flight, is side-effecting** (`authz.side_effecting_call`, the
  predicate the plan gate and dry-run use, plus a handoff). A turn that acted ends `interrupted`: an
  in-flight call may have taken effect, and the audit rows of the calls that finished (batched
  until the turn's end) and the turn's spent approval (`D-2026-10-08` records that window) died with
  the pod. Calls that only read are
  repeated; durable jobs and the calculation cache already make a repeated read cheap.
- **Who**: only the person who sent the turn (`session_messages.actor`), and the turn runs as that
  person on the attach request's own credential, with the roles that token carries now. A participant
  who did not send it gets the 404 an ended turn gets and leaves it alone. A waiting message in the
  line goes first and supersedes it; so does any new message (`settle_interrupted_turns(spare_resumable=False)`).
  A Stop on a dead turn ends it for good, as the chemist declining.
- **How**: the attach calls the same `_start_turn` a `POST …/messages` does, so claim, heartbeat,
  admission, the budget check, the plan gate, authorization and audit see an ordinary turn.
  `claim_to_resume` takes the session claim and sets `resumed_at` in one transaction: two replicas
  attaching at once resume it once, the other follows the first through the relay. One resume per
  turn: a step that kills its pod twice ends `interrupted`.
- **What it re-executes**: `astream(None, config)` runs the pending node. The committed tail is
  replayed through the reader a live update goes through, so the client, the trace, the grounding
  evidence and the transcript's tool exchanges are what an uninterrupted turn would hold.
- **Spend is booked once.** The dead attempt booked nothing (it died before `_finish_turn`, which
  precedes the answer), and the resumed turn books one row for the whole turn under the original
  correlation id: the model calls the dead attempt completed are in the checkpoint with their usage
  and are added; the call it died inside reported nothing. `dry_run` is kept from the original.
- **What is not preserved**: the per-turn counters of the loop and spend caps are untracked channels
  and restart for the resumed run; the one-resume bound caps that at one extra cap's worth.

Not taken here: the simplification of `turn_relay.py`, `detach.py`, `turn_remotes.py` and
`session_queue.py` that W3.6 names. Measured on the polling relay (defaults, two processes): an idle
holder costs 0.4 → 8.3 transactions/s as soon as it holds a turn, a remote watcher adds 8.0, a remote
attach takes 0.35 s against 0.05 s locally and a remote Stop 0.32 s against 0.07 s. A `LISTEN`
wake-up would cut the first two and the last two, but it adds a session-mode connection per replica
(`D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint`) and does not by itself
remove the frame table (an `exhibit_draft` frame exceeds a notification's payload, the first decline
of `D-2026-10-04`). It is a BACKLOG row with these numbers.

## Consequences

- No model-facing text and no wire shape changed: the resumed turn streams the events of any turn on
  the attach's response, under `X-Chemclaw-Turn-Correlation-Id` of the original turn. Only
  `watch_turn`'s description changed (contract 1.0.1). `Chemclaw3_ui` follows a reloaded turn through
  the same route and needs nothing, but a client whose stream just dropped **polls the transcript
  instead of attaching**, so the resume starts when the page reloads or the UI attaches after a
  drop; a UI change to attach after a cut stream is what makes it prompt.
- A turn the sender never returns to stays `running` for up to `service_turn_timeout_seconds`, then
  ends `interrupted` for the next toucher as before; a non-sender sees it `running` meanwhile.
- A resumed turn's audit trail has no rows for the reads the dead attempt made.
- Counters: `chemclaw_turns_resumed_total`, `chemclaw_turn_resume_refused_total{reason}`.
- Mixed versions: a turn started by a pod without this change has no question id in its thread and is
  never resumed.

Revisit when: a deployment's `chemclaw_turn_resume_refused_total{reason="acted"}` is a large share of
`chemclaw_turns_finished_total{outcome="interrupted"}` (state-changing tools that are idempotent by
construction, with a key, could then be allowed past the boundary); or the share of resumable turns
that end `interrupted` at the window rather than resumed is high with a UI that attaches (a sweeper
needs a stored identity first).

## What keeps it true

`tests/test_turn_survives_pod.py` kills real processes at named points: between model calls (resumed
once, final answer once, one cost row, the sender's identity), inside a state-changing call and after
a finished one (not repeated, `interrupted`), inside a read (repeated — the control), a member's attach
(left alone — the control), two attaches at once (one run), a Stop (never resumed), a new message
(supersedes), a live turn followed from another replica (unaffected) and a turn that dies again
(not resumed twice). `tests/test_turn_resume.py` holds the thread rules and the claim, window and
booking rules on a migrated database.
