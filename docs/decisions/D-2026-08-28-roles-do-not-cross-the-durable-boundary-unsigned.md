# D-2026-08-28-roles-do-not-cross-the-durable-boundary-unsigned — durable authorization does not trust payload roles

## Status
Accepted.

## Context
A security review found that the durable layer's authorization rested on an unstated assumption:
that only trusted code can write to the Temporal broker. Three binders lifted a **role set** out of
an activity's own argument model and stamped it into the ambient identity contextvar that
`agent/authz._has_required_role` reads — `durable/interceptor.activity_context`,
`durable/report_workflow.retrieve_section`, and `durable/template_activities._acting_as`. A workflow
argument is data the broker relays, not a verified claim, so anyone able to enqueue an activity (a
plaintext broker, a compromised worker, a replay, a Temporal-API operator) could put
`roles=["Chemclaw.Privileged"]` in the payload and satisfy the privileged gate — full impersonation,
with the audit trail then affirmatively attributing the action to the impersonated user. The
docstrings defended only the *empty* case ("every gate fails closed on an empty set"); the code took
a *non-empty* set.

True server-side re-resolution — the obvious fix — is not available here: it needs either an Entra
Graph lookup (declined by D-089) or a signed payload (a Temporal payload codec, a new subsystem).

## Decision
Bind roles from the payload **only where a legitimate use of them cannot be told apart from a
forgery is impossible** — i.e. nowhere the roles are pure risk — and keep them exactly at the one
boundary where refusing them would break a shipped capability instead of closing a hole:

- **Interceptor (`activity_context`)** — binds the actor for attribution, roles **empty**. Every
  non-template activity gets no role authority from its payload. A connector job that legitimately
  needs a user's entitlements was already authorized at the front door before the workflow started
  (`connectors/jobs.prepare_job_launch`), so the in-activity check is defense-in-depth and fails
  closed.
- **Report retrieval (`retrieve_section`)** — actor crosses (so a gated source is not silently
  skipped for lack of any identity, and the run is attributed), roles **empty**. An
  entitlement-gated share now stays out of a report for a defensible reason rather than a forgeable
  one.
- **Template authorize path (`_acting_as`)** — binds `StepIdentity.roles`. This is the *first*
  authorization for a template step (a step launched by another step has no front-door pre-check),
  so binding empty would refuse every entitled template job rather than fail closed on a forgery.
  This one path trusts the payload, and what it relies on is that only trusted code can enqueue a
  `TemplateWorkflow` — broker write access restricted by Temporal mTLS, enforced under
  `entra_required` by the durable-TLS guard.

## Consequences
- The full-impersonation chain is closed on the interceptor and report paths regardless of broker
  posture.
- The template-launches-a-privileged-step case remains reliant on broker authentication (mTLS). Its
  residual is recorded here rather than hidden: fully closing it without breaking entitled template
  jobs requires a signed identity payload (a Temporal codec), which is a separate decision.
- Role-scoped **durable retrieval** (a report reading an entitlement-gated share on behalf of its
  requester) no longer works, because that authority no longer crosses the boundary. Restoring it
  needs the same signed payload.
- `tests/test_report_workflow.py` and `tests/test_durable_observability.py` now pin the empty-role
  contract; `tests/test_template_job_step.py` pins that an entitled requester still passes the
  template gate.
