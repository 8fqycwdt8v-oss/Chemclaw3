# D-168 — A template step runs as its requester, and four steps that had never run

**Status:** accepted · **Closes:** DARK-2

## Context

`durable/template_activities.py` opens by stating its own purpose:

> A template must not become a way to run a tool the requester could not run directly, and this is
> where that is enforced.

Two lines of the module enforced it — the in-process branch of `_invoke`, which hand-applies
`make_audit_middleware` and `enforce_tool_authz`. The other two paths did not:

- **the connector branch**, three lines below, called `connector.call_tool(...)` and reached the
  connector directly. So both tool steps of the shipped `hazard-briefing` left no GxP audit row, and
  a template naming a role-gated connector tool ran it for anyone who could start the template;
- **the job step**: `ResolvedJob` carried the connector, workflow and queue and dropped `expensive`
  and `precondition` on the floor, and `_run_job_step` started the child workflow with
  `resolve(step.arguments, scope)` exactly as written. A template naming `compute_dft_energy`
  started HPC work for anyone entitled to run its `run_<name>` tool; a job's declared domain
  guard — the one `JobSpec.precondition` documents as having no other replay-safe home — never ran
  on this path; and the launch was unaudited and unvalidated.

## Decision

**A template step runs with the requester's entitlements**, which is what the module already
claimed. The alternative — a declared service identity — would make a template a
privilege-escalation surface by design, and would have to be written down in the manifest and
audited as such. Neither is what the templates in this repo are for.

### The connector branch

Routed through the same `_call_function_tool` as the in-process branch. MAF's MCP tools are ordinary
`FunctionTool`s, so nothing about the call had to change except the decision to govern it.

### The job step

The pre-flight becomes **one function with two callers** rather than two functions with one
implementation. `connectors/jobs.prepare_job_launch` holds validate → `authorize_trigger` →
precondition → serialize; `build_job_tool` calls it (that code moved, unchanged); and
`resolve_job_step` becomes `authorize_job_step`, which stamps the step's identity, runs the same
pre-flight, and returns the **validated payload** as part of `ResolvedJob`. There is no representable
state in which the workflow holds a `ResolvedJob` and has not passed the gate.

Duplicating four lines would have fixed today's instance and left the next launcher to rediscover
it. The audit row goes through `make_audit_middleware` over a real `FunctionTool` named for the job,
not a second `AuditEvent` emitter — one place decides what an audit record looks like, and the row
now reads the same as a chat turn's launch of the same job.

A refusal raises `AuthorizationError` (a `ValueError`), which `BAD_DATA_RETRY` lists non-retryable,
so an unentitled step fails on its first attempt naming the reason rather than retrying an
authorization decision that will never change.

## What running it live found

The offline suite went green on the above. Running the shipped template against a real Temporal
server then failed four times in a row, each time on something different, and each time on something
that had never executed:

1. **`run_tool_step` and `run_agent_step` were registered on no worker.** Only the job-step resolver
   carried `@durable_activity`; the other two had a bare `@activity.defn`. `hazard-briefing` failed
   on its *first* step with "Activity function run_tool_step … is not registered on this worker".
2. **A `tool` step's result could never cross the activity boundary.** `invoke` wraps a result in
   `list[Content]`, which Temporal's converter refuses: *"Unable to serialize unknown type"*. Both
   branches, so no template with a tool step had ever completed a run. Results are now rendered to
   what the converter can carry — which is also what `${steps.<id>.result}` should hold: the tool's
   answer, not the framework's envelope around it.
3. **An `agent` step could not run under `harness_enabled`** — the configuration the Helm chart
   sets. It calls `agent.run` with no session, which the harness middleware refuses ("ToolApproval
   Middleware requires an AgentSession"), the same wall D-152 hit for the CLI.
4. …and the fix for (3) is to run the step **without the harness**, not to invent a session. A
   template exists to fix the sequence; a planning loop inside one of its steps would give the step
   back the discretion the template was written to remove, and under D-167's gate would refuse every
   write inside it for want of a plan nobody can approve. `_classic` resolves the step's profile and
   switches only that dimension off, so every other narrowing survives.

`_params_model` also had to be memoized per (connector, job definition): `create_model` returns a
fresh class each call, so the moment the pre-flight moved out of `build_job_tool`'s closure it began
generating a second class per job and `model_validate` rejected specs built from the other one.

## Consequences

- A chemist who cannot run `compute_dft_energy` directly cannot run a template that does.
- Every template step now appears in `audit_events` under the requester's oid, with the run's
  workflow id as its correlation id, so "what did this procedure do, for whom" has an answer.
- Templates work at all, which is new.

## Verification

Counterfactual: restoring `connector.call_tool` and the raw payload fails seven tests across two
files — audit rows, the RBAC refusal, the expensive gate, the declared precondition and argument
validation. Removing the worker registration fails its own test.

Live: 6/6 — `hazard-briefing` completes its three steps, both connector tool steps leave GxP rows
under the requester with the run as correlation id, and an expensive job step is refused for an
unentitled requester before any child workflow starts.
