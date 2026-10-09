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
  than `service_turn_timeout_seconds` (a turn that old would have timed out), no outcome booked for
  its correlation id, and a recorded `question_id`. Migrations 126 and 127 add `resumed_at`,
  `dry_run` and `question_id`.
- **The question's identity is the server's.** `run_turn` mints the message id the chemist's message
  has in the thread, keeps it on the question's row, and a resume finds the question by it. It is
  not the correlation id: a client may present any well-formed `X-Chemclaw-Correlation-Id`, and the
  thread replaces a message that repeats an id, so a member presenting the sender's turn id would
  have overwritten the sender's question in what the model reads. This holds for every turn, not
  only a resumed one. A turn begun by the previous image has no recorded id and is never resumed;
  transcripts and checkpoints written before keep loading unchanged.
- **Judged** (`turn_resume.judge`) on the checkpointed thread: the question is found by that id, and
  after it there are only the model's tool calls and their results, the last assistant message
  possibly still waiting for results. **Every call, finished or in flight, must be on a positive
  list** (`authz.repeatable_call`): the harness's planning and filesystem verbs (not a write under
  `/memories`), the in-process reads of `READ_ONLY_TOOLS` except `NOT_REPEATABLE_READS`, and the
  tools an enabled manifest of this process classifies `read_only`. Anything else is `acted` and the
  turn ends `interrupted`: an unknown name, a `task` helper (a thread of calls of its own), a job
  launcher, a handoff, a tool of a connector this process does not have. An in-flight call may have
  taken effect, and the audit rows of the calls that finished (batched until the turn's end) and the
  turn's spent approval (`D-2026-10-08` records that window) died with the pod.
  `NOT_REPEATABLE_READS` is `create_exhibit` and `revise_exhibit`, which write rows and which the
  plan gate rightly lets run (`D-2026-10-02`: an artefact is part of the answer, not an effect), and
  `condense_protocols`, which spends model calls the thread does not record. They are listed there
  and not reclassified as state-changing, because that would put artefacts behind plan approval,
  which is a different decision.
- **Who**: only the person who sent the turn (`session_messages.actor`), on the attach request's own
  credential, with the roles that token carries now. A participant who did not send it gets the
  404 an ended turn gets and leaves it alone. A waiting message in the line goes first and
  supersedes it; so does any new message (`settle_interrupted_turns(spare_resumable=False)`). A Stop
  from the sender or the owner on a dead turn ends it for good (200, the question `interrupted`) and
  answers truthfully: a resume that committed first leaves nothing to mark and the Stop then finds
  the turn running. An unload stop does not end it: the reload that follows is what resumes it.
- **How**: the attach calls the same `_start_turn` a `POST …/messages` does, so claim, heartbeat,
  admission, the budget check, the plan gate, authorization and audit see an ordinary turn.
  `claim_to_resume` takes the session claim and sets `resumed_at` in one transaction: two attaches at
  once resume it once and the loser follows the winner, on this replica or another. A reader that
  was waiting on the question's row lock (`_LOCK_LAPSED`) marks nothing from its old snapshot. The
  verdict that decided to resume may be up to `service_readiness_cache_seconds` old, so the thread
  is judged again after the claim (`resume_point_fresh`): from the claim on the old holder cannot
  write to it (below), so what is read then cannot change, and a thread that is no longer
  resumable is given back like any other resume that did not run.
  A resume that is shed, refused by the budget or fails before its first step gives the claim back
  *and* its mark (`give_back_resume`, one transaction), so the attach can be tried again: the claim
  is in effect taken for admission and returned if admission fails.
- **The old holder is fenced.** A pod that stalls past its lease (a blocked loop, a stopped
  container) wakes after another replica resumed the thread. Without a fence both drive one
  checkpoint: a state-changing call after the judgement runs twice, there are two answers and two
  cost rows, and a model call that was in flight in the stalled pod completes and LangGraph writes
  its checkpoint, newest by id, from a background executor that nothing cancels, so the next turn
  loads the stalled pod's fork. The fence has four consumers, one `TurnFence` (`core/turn_fence.py`)
  per turn:
  - **Checkpoint writes.** `SchemaStampedSaver` runs every write in one transaction that first takes
    `SELECT … FROM session_turns WHERE holder = … AND expires_at > now() FOR SHARE`. A takeover is an
    `INSERT … ON CONFLICT DO UPDATE` of that row and waits for the lock, so a write either commits
    before the takeover (the new holder then reads it) or finds no row and is refused with
    `TurnFenceLost`: there is no gap between the check and the write. Reads are not fenced.
  - **Every model call** asks the claim before it is made (`HoldClaimBeforeModel`), so a woken pod
    stops before it spends.
  - **Every call that is not repeatable**, including each of several in parallel and those of a
    `task` helper, asks immediately before it runs (`refuse_when_claim_lost`, innermost in the
    chain), with a margin: the claim must outlive the check by a third of the lease
    (`_TURN_OWNS`), which a healthy holder always has since it refreshes three times per lease.
  - **The heartbeat** loses the fence when a refresh matches no row, or when no refresh has
    succeeded for a lease. It bounds a pod that is doing nothing effectful (a stuck read), at about
    a lease plus two refresh intervals after the last success; effects and checkpoints are stopped
    by the two checks above, which do not depend on that timer.
  A fence is lost only on an answer, never on an error. `hold()` distinguishes *taken over* (the
  claim names someone else, or no row: the fence is lost, the pump cancelled, the teardown settles
  nothing, books nothing and spends no approval) from *could not ask* (the store raised; one retry,
  then `ClaimUnverifiable`). For the second, the call is refused with a message the model can
  read (fail closed for effects) and the turn is not discarded: it books its spend and settles
  honestly, because a store outage is not evidence of a takeover. `run_turn` looks once more before
  it writes its ending, and a lost fence also skips `_escalate_exhausted_review`. What the fence
  cannot cover: an effect already issued when the claim was lost (a request on the wire, a job
  started). A claim live with that margin at the check means no replica can have taken the thread
  before the call returns, unless it outlasts the remaining lease, which is the exposure the lease
  has always had.
- **What it re-executes**: `astream(None, config)` runs the pending node. The committed tail is
  read as a live update is (`replayed_events`): the trace, the grounding evidence, the transcript's
  tool exchanges and the spend are what an uninterrupted turn would hold, and the events are
  counted but **not sent**. An attach shows a turn from the moment of attaching, as a local watcher
  does; the page that was cut off already has the earlier frames, which carry no ids a client could
  de-duplicate them by, and a page that reloaded has none to repeat. The answer's text arrives whole
  in the `answer` frame. A call the thread marks failed or refused (`FAILED_CALL_MARK`, stamped
  where this system answers a failure) stays out of the evidence, as it does live. No `plan` event
  is replayed: the plan card is read from the checkpoint by its route, and the approval request is
  raised at the end of the turn, as for any turn.
- **Spend is booked once.** The dead attempt booked nothing (it died before `_finish_turn`, which
  precedes the answer), and the resumed turn books one row for the whole turn under the original
  correlation id: the model calls the dead attempt completed are in the checkpoint with their usage
  and are added; the call it died inside reported nothing. What the thread cannot show is
  under-booked, and the bound is stated: the in-flight call; the model calls of a compaction the
  dead attempt ran (a turn on a context near the compaction threshold); and the failure and refusal
  counts of the earlier part. A `task` helper and `condense_protocols` are not on the list, so a
  turn that used them is not resumed rather than under-booked. The one-resume bound makes the
  compaction residual a single attempt's worth, and the resumed run is under the caps and the
  per-user window as any turn; the runaway cap counts the thread's assistant messages, so the
  earlier part still counts against it. `dry_run` is kept. The notice that tells the model which
  artefacts the chemist edited (`mark_told`) is skipped on a resume, so those edits are told again
  at the next turn rather than lost.

Not taken here: the simplification of `turn_relay.py`, `detach.py`, `turn_remotes.py` and
`session_queue.py` that W3.6 names. Measured on the polling relay (defaults, two processes): an idle
holder costs 0.4 → 8.3 transactions/s as soon as it holds a turn, a remote watcher adds 8.0, a remote
attach takes 0.35 s against 0.05 s locally and a remote Stop 0.32 s against 0.07 s. A `LISTEN`
wake-up would cut the first two and the last two, but it adds a session-mode connection per replica
(`D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint`) and does not by itself
remove the frame table (an `exhibit_draft` frame exceeds a notification's payload, the first decline
of `D-2026-10-04`). It is a BACKLOG row with these numbers.

## Consequences

- No model-facing text and no event shape changed. `GET …/turn/stream` can now answer with the
  limits of a new turn (429, `queued`, `at_capacity`, `budget_exhausted`) and `POST …/turn/stop` can
  answer 200 for a dead turn where it answered 404; both are in the routes' descriptions
  (contract 1.0.2) and add no field. A resumed turn is under `X-Chemclaw-Turn-Correlation-Id` of the
  original. `Chemclaw3_ui` follows a reloaded turn through the same route and needs nothing, but a
  client whose stream just dropped **polls the transcript instead of attaching**, so the resume
  starts when the page reloads or the UI attaches after a drop.
- A turn the sender never returns to stays `running` for up to `service_turn_timeout_seconds`, then
  ends `interrupted` for the next toucher as before; a non-sender sees it `running` meanwhile.
- A turn that lost its session is ended by the pod that lost it, whether or not it was ever
  resumed: a lease that lapses under a live pod now ends that turn instead of letting it run beside
  whoever started the next one. `chemclaw_turn_claims_lost_total` counts it.
- A resumed turn's audit trail has no rows for the reads the dead attempt made.
- Counters: `chemclaw_turns_resumed_total`, `chemclaw_turn_resume_refused_total{reason}` (once per
  turn, when it is marked). `chemclaw_turns_started_total` counts a turn once, at its first attempt.
- **Rolling update.** A pod still on the previous image has neither the lock nor `resumed_at` in its
  predicate (`claimed_at <= created_at`): it does not spare a resumable turn, and it can mark a
  resumed turn `interrupted` while the resume runs. The resumed turn then finishes and its answer
  overrides the mark (`D-2026-10-03`: an answer overrides), so the cost is a transcript that reads
  `interrupted` for the length of the turn. A turn that pod began has no question id and is never
  resumed. Exact once every pod runs this image.

Revisit when: a deployment's `chemclaw_turn_resume_refused_total{reason="acted"}` is a large share of
`chemclaw_turns_finished_total{outcome="interrupted"}` (state-changing tools that are idempotent by
construction, with a key, could then be allowed past the boundary); or the share of resumable turns
that end `interrupted` at the window rather than resumed is high with a UI that attaches (a sweeper
needs a stored identity first).

## What keeps it true

`tests/test_turn_survives_pod.py` kills and stops real processes at named points: between model
calls (resumed once, final answer once, one cost row, the sender's identity), inside a state-changing
call and after a finished one (not repeated, `interrupted`), inside a read (repeated — the control),
a member's attach (left alone — the control), two attaches at once on one replica and on two (one
run, the other follows to the answer), a Stop (never resumed), a new message (supersedes), a live
turn followed from another replica (unaffected), a turn that dies again (not resumed twice), a pod
stopped past its lease and woken after its turn was resumed *and finished* while it sat mid-model-call
(its state-changing call does not run, nothing is booked, and the newest checkpoint, which the next
turn loads, is still the resumed turn's; the control strips every fence from that pod and the
thread forks; a shorter stall is not fenced either) and a member presenting the sender's correlation
id (both questions stay in the thread). `tests/test_turn_resume.py` holds the thread rules (the
positive list, fail closed), the claim, window, give-back and booking rules, the race of a reader
against a resume on two connections (with the bare statement as the control), the saver against a
held, a taken-over and a lapsed claim (with the permissive statement as the control) and the replay.
`tests/test_turn_fence.py` holds the fence: the tri-state `hold()`, a refused call on an error, each
of parallel calls and a helper's calls checked, and the heartbeat's two ways to lose.
