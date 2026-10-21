# D-2026-10-21-a-job-ending-may-wake-a-follow-up-turn — a launcher may opt a job into one read-only agent turn on its failure or completion, run under the identity pinned at launch

**Status:** proposed · **Date:** 2026-10-21

## Context

No path starts an agent turn without a human message. A job's ending reaches the chemist as a
`job_completed` or `job_failed` session event, and it reaches the model only appended to the
*next* human message (`api/runner.py::_with_pushed_job_results`).

A long job that fails hours later therefore waits for a person to come back and ask what went
wrong. A typical failure is SCF non-convergence, walltime exceeded or a lost node. The diagnosis
and a corrected resubmission are exactly what the agent is for.

`durable/check_in.py` runs no model, and its reason is that a Schedule has no `StepIdentity`.
Synthesizing one "would be the system acting as a chemist on a timer". A template's `agent` step
already runs a governed turn without a chat, under a `StepIdentity` pinned at launch
(`durable/template_activities.run_agent_step`).

## Options

1. **No reasoning on job events** (status quo).
2. **Follow-up turns.**
   - The launcher chooses `follow_up: none | on_failure | on_completion` at launch, under the plan
     gate.
   - The job's `ConnectorJobInput` carries a `StepIdentity` snapshot.
   - On the event, one turn runs through the `run_agent_step` machinery under the `computation`
     profile, narrowed to read-only, plus one new `propose_resubmission` tool.
   - That tool creates a pending action the chemist approves; it can never launch anything.
3. **A standing supervisor agent** watching all jobs. The name and the idea were retired in
   D-2026-08-12-a-supervisor-that-holds-every-tool-has-no-reason-to-delegate. It would also act for
   no one in particular.

## Decision

**Option 2.** This keeps check_in's reasoning rather than overturning it: the identity is the
launcher's own, given with consent at launch, not one synthesized on a timer.
- Follow-ups are capped per job by `follow_up_max_per_job` (default 2) and charged to spend.
- The answer lands in the session as an agent message marked `origin: follow_up`, with the pending
  resubmission beside it.

## Consequences

- `ConnectorJobWorkflow._finish` and `_notify_failure` start `durable/follow_up.py::FollowUpWorkflow`
  when opted in.
- A test over the follow-up turn's bound surface asserts it holds no side-effecting tool.
- Measured by the compute eval: at least 8 correct resubmission proposals in 10 failure probes on
  the mock.
- Plan item: W14. Depends on W5.9 and W5.13.
