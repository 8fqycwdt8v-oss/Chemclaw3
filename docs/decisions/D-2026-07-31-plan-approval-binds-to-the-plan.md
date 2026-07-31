# D-2026-07-31-plan-approval-binds-to-the-plan — The plan approval binds to the plan, not to the session

**Status:** accepted · **Date:** 2026-07-31 · **Extends:** D-137

## Context

D-137 built the pre-execution approval gate the documents had always described: MAF advertises a
`mode_set` tool to the model, so the agent was flipping itself out of plan mode and the audit trail
recorded that under the asking chemist's Entra oid. D-137 retracted the tool, moved the flip to an
owner-scoped HTTP route, and — the part that matters here — keyed the durable `plan_approvals` row
on `(session_id, plan_hash)`, reasoning explicitly that

> An approval that only recorded "this session may execute" would authorize whatever the plan later
> became: the agent could present a modest plan, have it approved, rewrite its todo list, and run
> something else under the same authorization.

That reasoning is right and the record was built correctly. **Nothing consulted it at execution
time.**

`set_agent_mode` had exactly one call site in the whole tree — `grant_execute` — and no
counterpart. Once `POST /sessions/{id}/plan/decision` flipped a session into execute mode, nothing
ever flipped it back. The only consumer of the mode was
`loop_should_continue=todos_remaining(looping_modes=["execute"])`, which asks "is this session in
execute mode?" and never "is this the plan that was approved?".

So the hash binding protected the *first* plan of a session and nothing after it. Turn two's
brand-new todo list looped in execute mode with no human in the loop, under an approval given for
different work. That is the precise scenario D-137's own docstring describes as the thing to
prevent, and it defeated the posture the Helm chart ships (`harness_autonomy=plan_only`).

A rejection had the mirror problem. `plan_approvals` keeps every decision and the read path takes
the latest, deliberately, so that clicking "no" after "yes" revokes — but the route only ever
called `grant_execute`, so a rejection wrote a durable row saying "rejected" while the session
carried on executing.

The escalation was partially masked by accident: the mode lives in in-process session state, so an
LRU eviction resets it. That is not a control, and under `session_store=postgres` it also means the
authorization silently varies with cache pressure.

## Decision

**An approval authorizes a plan, and the loop checks it.**

- `grant_execute` records the authorized plan beside the mode, in the session's own state under
  `chemclaw_plan_approval` — its own key rather than inside MAF's `agent_mode` dict, so this repo's
  data is not a hostage to upstream's schema. Same lifetime as the mode itself, so no new way for
  the two to disagree is introduced.
- `plan_bound` wraps the loop predicate: it refuses when the plan the session is proposing now is
  not the plan that was authorized, then defers to MAF's `todos_remaining`. Composed around
  upstream rather than replacing it, for the same reason `PlanApprovalModeProvider` retracts one
  tool instead of rewriting `before_run` — a reimplementation silently loses whatever upstream adds
  next. The inner result is passed through unchanged rather than coerced to `bool`, because MAF
  allows `(False, reason)` and flattening it would discard the explanation.
- `revoke_execute` returns the session to plan mode and drops the authorization, so a rejection
  after an approval means what the durable row says it means.

**Two hashes, because "what was shown" and "which plan is this" are different questions.**
`current_plan_hash` stays as it was — over the rendered `[x]`/`[ ]` lines, so the approval handshake
cannot authorize something other than what the chemist saw, and ticking steps off correctly makes
it a new plan to re-approve. A new `plan_identity_hash` is over the step titles alone
(`harness_todo.todo_steps`).

Binding *execution* to the displayed hash would have been the obvious implementation and is wrong:
it revokes the approval the moment the first step completes, so the loop stops after one iteration,
every time. What must revoke an authorization is the plan being **rewritten**, not progress through
it. Two functions rather than one with a flag, because the two questions have different answers and
a caller should have to say which it is asking.

**The binding applies only under `harness_autonomy="plan_only"`.** Under `execute` the operator has
declared there is no approval gate; binding the loop to an approval that will never be granted
would not harden that deployment, it would stop it from ever looping.

## Consequences

The gate the documents describe is now the gate that runs, for every plan in a session rather than
the first.

The regression is pinned behaviourally in `tests/test_agent.py`, and the discriminating case is the
**rewrite** rather than the unapproved session — an unapproved session is also still in plan mode,
so `todos_remaining` alone already refuses it and a test that stopped there passes with the binding
removed. That was verified by removing the binding and watching the test go green, which is how the
first version of it was found to be worthless.

**The authorization is still in-process, and that is now the largest remaining weakness here.** The
approved plan lives in session state, so an LRU eviction or a pod restart drops it — fail-safe in
direction (execution stops) but it means a chemist may be asked to re-approve for reasons that have
nothing to do with the plan, and it means the durable `plan_approvals` row still points at a
`plan_hash` whose subject exists nowhere durable. Making the plan itself durable is a schema change
with its own design (a `session_plans` table, read back on rehydration) and is tracked in
`BACKLOG.md` rather than smuggled in here.
