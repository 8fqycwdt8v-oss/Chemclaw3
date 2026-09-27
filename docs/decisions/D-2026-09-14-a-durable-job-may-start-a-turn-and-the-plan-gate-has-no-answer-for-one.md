# D-2026-09-14-a-durable-job-may-start-a-turn-and-the-plan-gate-has-no-answer-for-one — an unattended turn needs an identity that is not a person and a gate that is not "wait for one"

## Status

Accepted. Depends on `D-2026-09-14-a-turn-outlives-its-request-already-and-nothing-can-pick-it-up`
for the lease it takes.

## Context

The second half of Wave 2 is a durable job starting a conversation turn — a Schedule noticing
something, a finished campaign asking the next question, a wait being answered and the work
continuing. Two controls stand in the way, and both were built on the premise that a turn has a
human at the other end of it. Neither is wrong; both need an answer rather than an exception.

**`require_actor` rejects if absent, and that is the core F4 rule.** Under `entra_required` a
trigger with no authenticated user is refused before any durable work starts — at roughly fifteen
call sites, not centrally: every durable job launch, every template run, every pending request,
every preference and subscription write, every knowledge note's `reported_by`. Off `entra_required`
it degrades to `service_actor_id`, which is right for local development and is not an answer for a
production deployment.

A Temporal workflow today carries whatever actor string its payload holds and **never any roles**:
`durable/interceptor.py` binds `roles=frozenset()` deliberately, because a workflow argument is data
the broker relays rather than a verified claim, and every gate treats an empty set as fail-closed.
The one exception is the template path, where `_acting_as` binds the real role set because
`authorize_job_step` is the first authorization for that step and broker write access is the
control. So an unattended turn starting today would run with an actor and no roles, and
`DEFAULT_WRITE_TOOL_GATES` would refuse every write requiring `entra_privileged_role_set`.

**The plan gate has no answer for an unattended turn, and its two branches are both wrong for one.**
`gate_applies` is `harness_enabled AND autonomy == "plan_only"`, and both ship on. With a
`session_id` and no human, every side-effecting call is refused *forever* — no timeout, no
auto-approve, no escalation — and the turn ends emitting an `ApprovalRequestEvent` into a stream
nobody is reading. With **no** `session_id` the gate is bypassed entirely at
`plan_gate.py`'s `if not session_id: return await handler(request)`. That is why
`run_agent_step` forces `harness_enabled=False` rather than running session-less under it: the
bypass is sound for a template step, whose authorization happened at `authorize_job_step`, and it is
not a licence to start sessionful unattended turns through the same hole.

## Decision

**A durable job may start a turn, and it does so as an attenuation of the actor that caused it —
never as a new one.** This is `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`'s rule
applied one layer out, and the reasons are the same: a turn that could name its own actor is an
unauthenticated write path wearing an authorization system.

1. **Identity comes from the job's own launch, which already carries it.** Every durable launch
   passes `require_actor()`'s answer as `requested_by`, and the interceptor binds it. A turn started
   by that job runs as that actor. A Schedule with no launching human does not get a turn at all
   until a decision names what it runs as — that is deliberately left open here rather than
   answered with `service_actor_id`, because the honest options (a service principal with a real
   role set, or refusing) differ in blast radius and the choice belongs with whoever configures a
   tenant.
2. **Roles do not cross the broker, and this does not change that.** An unattended turn carries the
   empty role set the interceptor binds, and therefore cannot call a role-gated write. Widening that
   needs a signed payload — a Temporal codec — which `D-2026-08-28-roles-do-not-cross-the-durable-
   boundary-unsigned` already scoped and is not this wave's.
3. **The plan gate is not bypassed and not auto-approved. An unattended turn runs `plan_only` and
   its refusals are the outcome.** A turn that cannot get its plan approved ends having done the
   reading and none of the writing, and what it produces is the plan — surfaced where a human will
   see it, which the durable wait already knows how to do. `HumanInTheLoopMiddleware` stays declined
   for the gate itself on
   `D-2026-08-15-the-plan-gate-stays-a-refusal-because-an-interrupt-cannot-ask-the-question`'s four
   measurements, and none of them is disturbed by this.
4. **It takes the durable claim before it starts, on the same terms a chat turn does.** Without it,
   a job-started turn on a live session interleaves with a chemist's turn and the DAG forks silently
   — measured, with one side's answer reaching its caller and never the session. This is the
   dependency on the first ADR and the reason the two ship together.

## Consequences

- **An unattended turn is a reader and a proposer, not a writer**, until either a signed role
  payload or a standing plan approval exists. That is a real limit and it is the safe direction:
  the failure mode of getting it wrong is an unauthenticated write path, and the failure mode of
  this is a turn that asks.
- **`run_agent_step` is not the turn starter.** It was the obvious candidate and it disqualifies
  itself on five counts, each deliberate: no checkpointer and no `thread_id` ("a second durability
  mechanism inside the first"), the harness forced off, no transcript write, no event stream, no
  plan gate. Reusing it means adding back the four things it removed on purpose, which is a
  different job than reuse.
- **The `awaiting` seam is the surfacing mechanism and already exists.** A turn that ends holding an
  unapproved plan has somewhere to put it: `AwaitAnswerWorkflow` holds a question open for a person
  with a deadline, an escalation and a projection into `pending_requests`, and since Wave 1 it also
  reaches a channel. No second primitive —
  `tests/test_awaiting.py::test_the_tree_still_has_exactly_one_durable_wait` says so and should stay
  true.
- **A `session_id`-less turn is not the escape hatch.** The gate's bypass on an absent session is
  sound where authorization already happened and is not a licence; a turn started this way has a
  session by construction, because it has a thread.

## What keeps it true

Owed by the build, each watchable as a mutation rather than a claim:

- A job-started turn with no actor is **refused** under `entra_required`, at the launch rather than
  at the first write.
- A job-started turn carries the empty role set, so a role-gated write refuses — asserted as an
  absence, so widening it without a codec turns red.
- A job-started turn on a session a chemist is using does not start, and says which.
- The plan gate fires for a job-started turn: `gate_applies` is true for its profile, and a
  side-effecting call without an approved plan is refused rather than bypassed for want of a
  session.
