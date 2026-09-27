# D-2026-09-14-a-turn-outlives-its-request-already-and-nothing-can-pick-it-up — the checkpoint holds everything a resume needs, which makes the three things it breaks the actual work

## Status

Accepted. The first of Wave 2's two decisions; the second
(`a-durable-job-may-start-a-turn`) depends on this one's lease.

## Context

The capability plan's Wave 2 is "a turn stops being an HTTP request", written on the assumption that
a turn is tied to the connection that started it and would have to become something Temporal drives.
Measured, both halves of that are wrong, and the correction makes the wave smaller and sharper.

**A turn already outlives its request.** `D-2026-08-27-a-disconnect-is-a-detach-not-a-stop` built
`api/detach.py`: the client going away detaches the *reader* while `DetachableTurn`'s pump task runs
`run_turn` to completion, and `service_turn_survives_disconnect` ships `True`. What it does not
outlive is the **process**. `RunningTurns` is a plain dict on `app.state`, so a pod restart kills
every turn in flight; `drain()` waits and then logs what did not finish. And there is no reattach at
all: events produced after a detach are discarded, so the tokens are gone and the client recovers
only the final answer row, and only once `_record_transcript` has run.

**A turn can be resumed from its checkpoint, today, with no new machinery.** Driven on the real
`SchemaStampedSaver` against Postgres, killing a turn mid-flight and resuming from a **separate OS
process** with a fresh graph and a fresh checkpointer: `graph.ainvoke(None, turn_config(T))` is the
whole API. Three kill points, none of which raised and none of which restarted from the beginning —
killed during the first model call, killed during the second with a tool result already in state,
and killed *inside* the tool. The message list comes back correct: one tool call, one matching
`ToolMessage`, no duplicates and no orphans. `interrupt()` is not needed and is not used.

So the durability this wave was going to build already exists. What does not exist is anything that
*calls* it, and three properties that a resume breaks.

## What a resume breaks, measured

**1. Tool execution is at-least-once across a pod death.** A tool killed mid-call is re-run on
resume with its original arguments. The checkpoint at that moment holds no `messages` or `files`
write for it — only a `__pregel_tasks` row carrying the `Send` that enqueues it — which is precisely
why re-execution is correct from LangGraph's side and a hazard from ours. Nothing in the tree needs
idempotency today because nothing resumes; `authz.side_effecting_tools()` is the set that would.

**2. Both per-turn caps are per-*invocation*, and the loop cap additionally under-counts.**
`model_calls` and `billed_tokens` are `UntrackedValue` subclasses, and `agent/state.py` already
states the consequence without drawing it: the channel "starts empty on every run of the graph
because there is nothing for the checkpoint to restore". A resume **is** a new run. Measured: a turn
that spent 100 billed tokens before dying comes back reporting only what the resuming process spent,
so a turn that dies *n* times gets *n+1* fresh `agent_max_turn_billed_tokens` allowances. The loop
cap is worse by one: resume re-enters at the *pending node*, after `enforce_loop_cap.before_model`
has already written its `branch:to:model`, so the call that was in flight at death is counted by
nobody.

The same mechanism skips the whole `before_model` chain for that one iteration — the compaction
edits included. Their writes are in the checkpoint, so LangGraph considers them done.

**3. Two writers on one thread fork the DAG silently.** No error, no warning, last writer wins.
Measured with two fresh turns started ~50 ms apart on one `thread_id` from two processes: 26
checkpoint rows with duplicate step numbers under different parents, one pod's question leaking into
the other's context, and the first pod's answer, tool call and tool result **absent from the
thread's tip** — returned to its caller and in no session. A live turn plus a second pod resuming
the same thread produced two different answers to one question, both billed, neither erroring.

The exclusion that prevents this today is entirely the front door's: an in-process `TurnLease` and
the durable `SessionTurnClaims` row, and the second exists only under `session_store="postgres"`.
`run_agent_step` takes neither, and nor does any other durable path.

## Decision

**A turn stays an in-process graph run over a Postgres checkpointer. It does not become a Temporal
workflow.** D-002 puts durability in Temporal for long and expensive *jobs*; a turn is neither, the
checkpointer already holds its state (D-2026-08-10 §3), and the measurement above shows the recovery
path is one call. Wrapping it would be a second durability mechanism inside the first, which is the
thing that rule forbids.

What this wave builds is therefore not durability but the three guards that make resuming it safe:

1. **A resume takes the durable claim first, and it must distinguish three states rather than two** —
   in flight elsewhere, orphaned, and already finished. The third is not cosmetic: `ainvoke(None)`
   on a completed thread is a silent no-op that returns the finished state with zero model calls, so
   a supervisor cannot tell "I resumed and completed it" from "there was nothing to resume" by the
   return value. `EmptyInputError` is what an *unknown* thread raises, which is a fourth.
2. **The per-turn caps are re-seeded from something durable on resume**, because per-turn-ness came
   from the channel and the channel's own guarantee is per-*run*. Whatever carries it must also
   account for the in-flight call the resumed iteration never counts.
3. **A side-effecting tool is idempotent across a resume, or the resume checks the pending task
   before re-entering it.** The `__pregel_tasks` row is readable, which makes the second option a
   real one rather than a hope.

## Consequences

- **The wave is smaller than planned and its risk moved.** No turn-starter workflow, no streaming
  channel out of an activity, no Temporal LangGraph plugin (already declined in
  `D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use`, whose premise — that its value is
  durable `interrupt()` and we use none — this measurement confirms rather than disturbs).
- **The spend cap moves into this wave with a second reason.** Wave 1 could not set
  `agent_max_turn_billed_tokens` because the number must come from `turn_costs` rows a database
  that has served nobody does not have. That is still true. What is new is that the cap is
  *structurally* per-invocation the moment resume exists, so the work is the re-seed regardless of
  what number a deployment eventually picks.
- **A reattach is now worth building and was not before.** The tokens a detached turn discards are
  reproducible from the checkpoint, so "the client recovers the answer from the transcript" stops
  being the only honest contract.
- **Nothing changes for a deployment until a caller exists.** No resume path ships in the commit
  carrying this record; it states what the next ones must hold.

## What keeps it true

Nothing yet, deliberately — this record scopes a build. What it owes, the build owes, and each is
an assertion a mutation can be watched against rather than a claim:

- A resume from a second process on a live thread is **refused** by the claim, and an orphaned one
  is not.
- A turn resumed after a kill reports the *sum* of both runs' model calls and billed tokens,
  including the call in flight when it died.
- A side-effecting tool interrupted mid-call is not executed twice end to end.
