# D-2026-08-27-a-step-runs-under-the-correlation-id-it-was-launched-with — the third ambient a template step never stamped

## Status

Accepted. Found in the same blind-spot audit as
`D-2026-08-27-a-start-to-close-timeout-does-not-bound-the-wait`.

## Context

`durable/template_activities.py::_acting_as` restores a template run's identity before each step
runs, because a worker has no request context. It bound two of the three ambients this system
carries per turn: the actor and the session id. It did not bind the **correlation id** — while
`StepIdentity` has carried one as a `min_length=1` field from the day it was written, with a comment
saying it "ties this run's audit events together, exactly as a conversation's correlation id does".

The function's own docstring described this exact bug class for the session id ("the actor was
stamped by all three step activities from the day they were written and the session by none"). This
is the third one, in the same place, unmentioned.

**The audit's premise about what it cost was half wrong, and measuring is what showed it.** The
claim under review was that every durable-job audit row booked `correlation_id='-'`. It did not:
`agent/audit.py` resolves `get_current_correlation_id() or correlation_id`, and both step
activities pass their `identity.correlation_id` explicitly into `invoke_governed` /
`make_audit_middleware`. A test asserting the audit row passed with the stamp removed, which is why
it is not the test that shipped.

What was actually unattributed is everything that reads the **ambient** and has no second source:

- `core/logging.py::ContextFilter` writes `correlation_id="-"` on every line a durable step logs —
  the on-call harm the audit named, and the one that is real.
- `kg/proposal.py::ambient_provenance` records an empty correlation id on every note a template
  proposes through the PR-gate, beside an actor and session that are correct.
- `connectors/jobs.py` reads the same getter for `ConnectorJobInput.correlation_id`, so a durable
  job launched from a template step was started with an empty one — losing the join for the whole
  downstream run, not just for the step.
- `connectors/identity.py::turn_headers` omits `X-Chemclaw-Correlation-Id` entirely when the
  ambient is absent, so a connector called from a template step could not join its records to ours.

## Decision

`_acting_as` binds all three ambients — actor, session, correlation id — as one bracket, and
unstamps them in reverse. One bracket because they are one fact: a step acts for a person, in a
chat, within one request. Three `set`/`reset` pairs written out at three call sites would be three
chances to forget a reset, which leaks one run's identity into whatever the worker picks up next.

The docstring is rewritten to say all of this, including which consumer was and was not affected.
It described two of the three stamps in the present tense while one was missing, which is the exact
shape of prose this repository has been burned by.

**The two other durable stamping sites are examined and deliberately left alone.**
`durable/memory_jobs.py::publish_memory_note_activity` stamps an actor and has no correlation id
available: `SynthesisUnit` carries none, and the synthesis jobs are system-triggered, so there is no
turn whose id it would be. `durable/report_workflow.py` (`retrieve_section`, `propose_report`) is
the same: `ReportRequest` and `SectionRequest` carry `requested_by` and `requested_roles` and no
correlation id, so the activity has nothing to bind. Inventing one — a workflow id dressed as a
correlation id — would make an unjoined run look joined, which is the failure
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` records at length. Threading a
real one into `ReportRequest`, as `ConnectorJobInput` already does, is a separate change with its
own launcher edit; it is a `BACKLOG.md` row rather than something smuggled in here.

## Consequences

- `tests/test_template_job_step.py::test_a_step_runs_under_the_correlation_id_its_run_was_launched_with`
  asserts the bracket through two real consumers — `ambient_provenance` (what the PR-gate records,
  and the same getter the job launcher reads) and `ContextFilter` (what a log line shows) — and
  asserts the teardown, because a bracket that leaks is worse than one that never stamped. It fails
  with the stamp removed; the audit trail is deliberately *not* asserted, because that assertion
  passes either way and would be evidence of nothing.
- A durable template run's log lines, proposals and launched jobs now carry the id that leads back
  to the turn that started them.

**`main` reached the same end by a wider route while this was in flight**, and the two now overlap.
`D-2026-08-27-a-job-that-fails-leaves-no-row` added `durable/interceptor.py`, which binds the same
three ambients around *every* activity on every worker, reading them from the activity's own
argument — one level into a nested `identity` field, which is exactly the shape the three step
inputs use. Measured against the real `ToolStepInput`, `AgentStepInput` and `JobStepInput`, it binds
the same actor, roles, session and correlation id this bracket binds, over a scope that strictly
contains it. On a worker, therefore, `_acting_as` is redundant in full — not only in the third
ambient this ADR added.

It is kept anyway, and the reason is stated rather than assumed: with the bracket neutered, four
tests fail, and two of them (`test_an_expensive_job_step_is_refused_for_an_unentitled_requester`
and `test_an_entitled_requester_passes_the_same_gate`) are the ones that prove a template step
cannot run a tool its requester could not run. They invoke the activity directly, where no
interceptor runs. Collapsing the two producers into one therefore means moving a security
control's proof onto a worker harness — a decision with its own blast radius, taken in its own
change (`docs/planning/BACKLOG.md`), not inside a merge. The two cannot drift in the meantime:
both read `StepIdentity`'s own fields, so there is one source of truth even though there are two
readers of it.
