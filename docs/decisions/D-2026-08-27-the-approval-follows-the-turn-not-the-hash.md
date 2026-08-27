# D-2026-08-27-the-approval-follows-the-turn-not-the-hash — what a plan approval authorizes, spent how it is used

## Status

Accepted. Refines D-167's one-turn rule at three edges a whole-engine audit found leaking; the
DARK-1 guarantees are unchanged and their tests still pass unmodified.

## Context

D-167 established that a human approval authorizes one turn's execution of one plan, identified by
the hash of its todo *contents*, spent durably at the turn's end. Three edges of that design read
correctly one call at a time and leaked across a turn:

1. **Consumption was hash-targeted.** `consume_turn_approval` hashed the plan *as it stood at turn
   end* and spent that approval. A turn that reworded its plan mid-flight hashed plan B, found no
   decision, and returned — leaving plan A's approval live indefinitely, re-authorizing any future
   turn whose todo list hashed back to A. Outside the one-turn limit, silently.

2. **The batch refusal livelocked the canonical shape.** A gated call arriving beside `write_todos`
   in one assistant message was refused outright, because `request.state` is a pre-batch snapshot
   and "which plan is this call part of" seemed unanswerable. But "tick the completed step and do
   the next one" is `TodoListMiddleware`'s own pattern — a status-flip `write_todos` beside every
   step's tool call — so an approved multi-step plan was refused on *every* step; the model's
   identical retry then tripped `refuse_repeated_calls`, and a turn could burn its whole loop
   allowance making no progress.

3. **An abandoned turn kept its approval armed**, on the argument that "a turn that was undone has
   not used its authorization." The premise is false the moment the turn has issued a
   state-changing call: durable jobs, note proposals and calibration rows are not rolled back by
   the teardown, so the authorization *was* used — and "drop the connection after the tools ran"
   became a way to act under one approval twice, the same shape as the token bypass that vetoed
   stream_events v3.

## Decision

**Judge a batched call against the plan the batch writes.** `plan_after_batch` reads the
`write_todos` arguments out of the same assistant message and the gate evaluates the call against
*that* plan's hash — the one answer to "which plan is this call part of" that holds under either
execution order, because the batch is atomic to the model. A status flip hashes identically
(`plan_identity` reads `content` only, the same property that lets an approved plan start a job
without revoking itself), so the canonical shape passes on its standing approval; a genuine
rewrite is approved or refused on its own hash, which is exactly D-167's question; anything
unanswerable — two rewrites in one batch, arguments the middleware itself would reject — still
refuses without asking the store. The DARK-1 batch (`write_todos(plan B)` beside a write, under
plan A's approval) still refuses, because plan B has no approval.

**Spend session-wide at turn end.** `consume_turn_approval` calls `consume_all(session_id)`:
every live approval the session holds is stamped, whatever identity the plan drifted to. "The
turn used its authorization" is a fact about the turn, not about the hash that survived to the end
of it. This also deletes the checkpoint read the old form paid to recompute a hash it no longer
needs, and with it the unreadable-plan branch and its metric — there is nothing left to fail to
read.

**Spend on abandonment when the turn acted.** The runner's cancellation path schedules a shielded
`consume_all` (the `turn_cost` no-await-in-teardown pattern) whenever the torn-down turn issued
any state-changing call — attempts included, because the conservative direction for an
authorization is to spend it: over-spending costs one extra approval click, under-spending is a
free second turn under a decision a person made once. A turn that only *read* keeps its approval,
which is the one-turn residual D-167 already accepts.

## Consequences

- `ApprovalStore.consume(session_id, plan_hash)` is replaced by `consume_all(session_id)` in both
  backends; no caller outside `plan_gate` existed.
- `chemclaw_plan_unreadable_total` is gone with the branch that ticked it.
- A chemist who approves plan A and plan B in one session spends both at the next turn's end.
  That is the intended narrowing: an approval is not a token to bank.
