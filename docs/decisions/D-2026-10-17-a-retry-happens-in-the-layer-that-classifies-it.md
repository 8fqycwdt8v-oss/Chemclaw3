# D-2026-10-17-a-retry-happens-in-the-layer-that-classifies-it — a failed call is retried by the layer that can tell bad data from an outage, and every layer around it makes one attempt

**Status:** accepted · **Date:** 2026-10-17 · **Builds on**
`D-2026-10-14-an-activity-states-its-retry-class`, which left the first two multiplications below
as backlog rows.

## Context

Three places retried one failed call in two layers, so the attempts multiplied and the outer layer
repeated work the inner one had already given up on. At the shipped defaults:

- **The hypothesis calculation child.** `HypothesisTournamentWorkflow._settle_jobs` started the
  `ConnectorJobWorkflow` wrapper under `child_workflow_retry` (`activity_max_attempts`, 5), on top
  of the `CALC` attempts of the job inside it: up to 25 attempts of one calculation, with every
  restart re-running the wrapper's records and push-back. The chat and template paths into the
  same wrapper already started it once. Working the row also showed the child was started on the
  bundle's queue, whose worker does not serve the wrapper, so it never ran at all.
- **A fan-out child.** `fan_out` restarted each child under the same policy. A
  `PublishNoteWorkflow` whose `NOTE_WRITE` activity spent its 3 attempts was restarted to 15, and a
  note write the store refuses (a `NoteWriteError`, bad data by name) was refused 5 times: a child
  failing through its activity surfaces with the child's failure outermost, so the bad-data list
  never matches at that boundary.
- **A tournament model call.** Each of the six model activities is `REMOTE` (5 attempts, 10 s
  doubling to 300 s, jittered), and inside each attempt the provider SDK retried
  `llm_max_retries` (3) times more: up to 20 requests for one call against a gateway that is down.

## Options

1. **Keep both layers and lower each bound until the product is acceptable.** No structural change.
   The product is what nobody reads, and the outer layer stays the one that cannot classify (a
   child boundary) or cannot space and spread (the SDK backs off for seconds, the same in every
   worker, and does not survive a worker restart).
2. **Retry in the outer layer and make the inner one single-attempt.** Children restart, their
   activities make one attempt; model activities make one attempt, the SDK retries. A child restart
   cannot tell bad data from an outage, so every deterministic refusal re-runs a whole child; model
   calls would need a class of their own and lose the outage ride-through `REMOTE` was chosen for.
3. **Retry in the layer that classifies; every layer around it makes one attempt.** An activity's
   class is that layer: a child workflow is started once (`child_workflow_once`, the only child
   policy left), and a model call inside an activity Temporal retries is built with no SDK retries
   (`build_chat_model(max_retries=0)`). Where Temporal makes one attempt, the SDK's retries are the
   one layer and stay: the template agent step (`GOVERNED`) and every call a turn makes.

## Decision

Option 3. The activity is the only layer that both classifies (the bad-data list is matched on the
activity's own failure) and spaces its retries for the dependency it calls, so it is the one that
retries; a layer around it can only repeat what it already gave up on. The tournament's
calculation child also moves to the tournament's own queue, so the wrapper runs where core's
worker serves it.

Searched for the same shape and left as it is: the embedding client keeps its SDK retries, because
inside a sweep (`note_index`, `document_sync`, `reembed`) the SDK retries one batch and Temporal the
whole sweep, which are different units, and retrieval absorbs a failed source in `sweep_sources`, so
Temporal never retries it.

**A template tool step is `GOVERNED`** (owner decision, 2026-10-17). `run_tool_step` calls one tool
through `invoke_governed`, the path `run_computable_check` takes, and was `REMOTE`, so a dropped
call was invoked again and audited again up to `activity_max_attempts` times for one step. A retry
must not repeat a governed tool call and its audit row, so it takes `agent_step_max_attempts`, one
by default, like the agent step and the hypothesis check. The three pre-flights that also go
through the governed chain (`authorize_job_step`, `ground_check_job`, `ground_check_template`) stay
`IO` for the reason `D-2026-10-14-an-activity-states-its-retry-class` gives: they start nothing,
and their extra audit row records the attempt.

## Consequences

`child_workflow_retry` is gone from `durable/retry.py`; `tests/test_retry.py` holds that every
child start in `src/` passes `child_workflow_once()`. A fan-out child whose activity spends its
attempts is now dropped and counted (`chemclaw_fan_out_children_dropped_total`) after them, so an
operator who wants a note write to ride out longer contention raises `note_write_max_attempts`,
which is the setting that says so. A tournament model call costs one request per activity attempt,
proven on the wire in `tests/test_hypothesis_tournament.py`, as is the calculation child's single
start on the served queue. A failing template tool step is invoked and audited once, proven through
`TemplateWorkflow` in `tests/test_templates.py`. A retry policy is not part of the replayed command
sequence, so no recorded history changes.

Revisit when: a child workflow must survive a failure none of its activities can retry, or a model
call moves into an activity Temporal does not retry.
