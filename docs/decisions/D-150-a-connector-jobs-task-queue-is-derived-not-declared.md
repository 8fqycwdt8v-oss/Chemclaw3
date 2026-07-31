# D-150 — A connector job's task queue is derived, not declared

D-149 removed `task_queue_for` and `JobRuntime` from `connectors/queues.py` as dead code, and
removing them exposed what they had been concealing: every `connector.yaml` declares a `task_queue`
per job that must equal `bundle_queue(connector)`; all eight declarations agreed; and **nothing
checked that they did**. A typo there is not an error anywhere — the job starts successfully, is
dispatched to a queue no worker polls, and waits forever. That is precisely the failure the module's
own docstring claimed to have designed out.

D-149 left it open on purpose, as a choice between two options that looked like a real trade-off:

- **(a)** `make connector-validate` asserts `job.task_queue == bundle_queue(connector)`.
- **(b)** Delete the field and derive it — strictly simpler, but apparently foreclosing the ability
  to route a connector-declared job onto core's `background-jobs` worker. That escape hatch was
  advertised in `JobSpec`'s docstring ("moving a workflow from core's worker to the connector's own
  is a one-line change here") and was what `task_queue_for`'s `background` branch existed for.

Deciding (b) therefore looked like deciding a capability question by side effect, which is why it
went to the backlog rather than into a cleanup commit.

## The trade-off does not exist

Following the dispatch path settles it. Core's background worker serves exactly this:

```python
BACKGROUND_WORKFLOWS: list[type] = registered_workflows("background")
```

and `durable/registry.py` is populated **at import time** — its own docstring is explicit that this
is the mechanism, not an accident: *"the isolation comes from the import boundary, not from the
decorator … an unimported module registers nothing."* Core's worker imports eight `chemclaw.durable`
modules and no connector bundle, and `tests/test_workflow_registry.py` asserts that boundary
directly.

So a job declaring `task_queue: background-jobs` would be dispatched to a queue on which its
workflow type is not registered. It would start cleanly and then sit there — the exact failure mode
the whole question is about, reached by using the feature as documented.

**The escape hatch was never open.** `task_queue` could hold one correct value and any number of
unrunnable ones. A field with one legal value is not a configuration point; it is a restatement of
something already known, and the only thing it can express is a mistake.

That also retires option (a) on its own terms: a validator asserting the field equals the
derivation proves the field carries no information. Adding a check to protect a value that cannot
legitimately vary is strictly worse than not asking for the value.

## What changed

`JobSpec.task_queue` is gone, and the queue is derived where the workflow is started:

- `connectors/jobs.py` — `task_queue=bundle_queue(connector)` when starting `ConnectorJobWorkflow`.
- `durable/template_activities.py` — the same, when a template step resolves a job.

Eight declarations across four manifests were removed, plus the fixtures in five test modules.
`ConnectorJobInput.task_queue` and `ResolvedJob.task_queue` are **unchanged**: they carry the
already-resolved value across a workflow boundary, and narrowing a durable input model buys nothing
here while being the one kind of change `docs/guides/workflow-versioning.md` cares about.

`tests/test_workers.py` had been carrying the only check that existed — `{job.task_queue for job in
jobs} == {queue}` — for `calc` and `qm`, and for those two bundles only; `bo` and the fixture had
none, and no rule covered a bundle added tomorrow. Both assertions are now vacuous and are gone. The
derivation is instead pinned where it actually matters, on the launch payload:
`tests/test_connector_jobs.py` asserts `payload.task_queue == "connector-calc"` for a manifest that
does not contain that string anywhere.

## The generalisation worth keeping

D-118 already established that a bundle's queue name is derived rather than declared three times.
This is the same argument reaching one place it had not: **a declaration whose only correct value is
computable is not a declaration, it is an opportunity.** The tell is when the fix for a stale or
unchecked field is a validator asserting it equals a derivation — at that point the honest change is
to delete the field, not to guard it.

It also refines D-149's closing observation. That ADR said the move on finding an unguarded
statement is to ask what would have caught it and add that. True, but incomplete: ask *first*
whether the statement needs to exist. Adding the guard was the wrong instinct here and would have
locked in the redundancy under the appearance of having fixed it.
