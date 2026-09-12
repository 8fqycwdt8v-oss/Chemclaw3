# D-2026-09-12-two-producers-of-one-identity-are-not-redundant-when-they-disagree — the template step's identity bracket stays, and the residual is the payload

**Context.** `docs/planning/BACKLOG.md` carried a row titled *"Two producers bind a template step's
ambient identity, and only one of them is needed"*. `durable/interceptor.py` binds the actor,
roles, session and correlation id around every activity on every worker, reading them out of the
same nested `identity` field that `durable/template_activities.py`'s `_acting_as` reads — over a
scope that strictly contains the bracket's. The row concluded the bracket was redundant on a
worker, that deleting it fails four tests, and that two of those four are the proof that a template
step cannot run a tool its requester could not run, so collapsing the producers would move a
security control's proof onto a worker harness.

**That is not the reason to keep it, and the row's premise is false.** Re-measured:

- `interceptor.activity_context` binds `roles=frozenset()` **deliberately** — a relayed workflow
  argument is data, not a verified claim, so binding a role from it would let anyone who can
  enqueue an activity forge a privileged role (`D-2026-08-28`).
- `_acting_as` binds the requester's real roles, also deliberately, and the eleven-line comment
  beside it says why: `authorize_job_step` is the *first* authorization a template step gets — a
  step launched by another step has no front-door pre-check to fall back on — so binding empty
  would refuse every entitled template job rather than fail closed on a forgery.

Three of the four values agree. `roles` does not, and the disagreement is the point of both
producers. That comment postdates the row and already stated it.

**The four failing tests do not mean what the row says either.** Neutering the whole bracket:

```
FAILED test_a_step_runs_under_the_correlation_id_its_run_was_launched_with
FAILED test_an_expensive_job_step_is_refused_for_an_unentitled_requester
FAILED test_an_entitled_requester_passes_the_same_gate
FAILED test_the_audit_row_names_the_session_the_run_was_launched_from
```

Neutering **only the role bind** — which is what collapsing onto the interceptor would actually
produce — leaves the refusal arm *still refusing* and fails the entitled arm outright. So the
collapse does not weaken a refusal; it refuses every legitimately entitled template job step. The
row's framing ("moving a security control's proof onto a worker harness") describes a cost that is
not the one the measurement finds.

**Decision.** The two producers are not redundant, must not be collapsed, and the row is deleted
rather than corrected — its title is its premise. What replaces it in `BACKLOG.md` is the residual
the comment already names and the row did not: **template step roles cross the durable boundary on
an unsigned payload.** `_acting_as` trusts `StepIdentity.roles`, and what makes that safe today is
that only trusted code can enqueue a `TemplateWorkflow` — broker write access restricted by
Temporal mTLS under `entra_required`. Closing it properly means a signed payload (a Temporal
codec), which is a new piece of work with its own trigger, not a deletion.

`_acting_as`'s own docstring said the interceptor "binds exactly what this bracket binds" and is
corrected in the same commit; that sentence was false about the one field the two disagree on,
which is the field the whole question turns on.

The same question still does not apply to `connectors/calc/activities.py::_acting_for`: the
interceptor skips plain string arguments by design, so it binds nothing there and that bracket is
the only producer on the calc job path.

## What keeps it true

| property | test |
| --- | --- |
| an unentitled requester is refused by `authorize_job_step`, and an entitled one passes the same gate — the pair that fails in opposite ways when the role bind is removed | `tests/test_template_job_step.py::test_an_expensive_job_step_is_refused_for_an_unentitled_requester`, `tests/test_template_job_step.py::test_an_entitled_requester_passes_the_same_gate` |
| the step runs under the correlation id and the session its run was launched with | `tests/test_template_job_step.py::test_a_step_runs_under_the_correlation_id_its_run_was_launched_with`, `tests/test_template_agent_step.py::test_the_audit_row_names_the_session_the_run_was_launched_from` |
| the interceptor binds no role from a relayed payload, on the real step models | `tests/test_durable_observability.py::test_the_real_template_step_inputs_are_the_shape_the_walk_reads`, `tests/test_durable_observability.py::test_a_model_authored_payload_cannot_supply_an_identity` |

Two mutations were driven: neutering the whole bracket (4 red, listed above) and neutering only the
role bind (the entitled arm red, the refusal arm green) — the second is the one that says what the
collapse would actually cost, and it is the arm the row never ran.
