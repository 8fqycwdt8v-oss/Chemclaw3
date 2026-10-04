# `chemclaw.durable` — Temporal durable execution

**Responsibility:** the durable lifecycle of core's own long jobs (ELN sync,
re-index, reports, memory synthesis), the one generic wrapper every
connector job runs inside (`connector_job.py`), and the worker process that hosts
them. Workflow code is deterministic and replayable; all I/O and non-determinism
lives in **activities**. Durability for long or expensive work lives here and **only**
here, never in the conversation layer's own stores — layer 1's checkpointer holds one
turn's state and no job's (D-2026-08-10 §3).

One core task queue (its name comes from `chemclaw.core.config`): `background-jobs`
(D-006). See `docs/reference/architektur.md` §2, §15.

**The workflows and the worker are one package** (D-148): `background_worker.py` hosts what this
package declares; a bundle's own `connectors/<name>/worker.py` is a different process on a
different queue.

**Which queue is a property of the capability, not of the deployment**, so it is
declared where the capability is defined rather than in the worker: put
`@durable_workflow("background")` above `@workflow.defn`, or
`@durable_activity("background")` above `@activity.defn`, and
`background_worker.py` serves it (`registry.py`, D-099). A name claimed by two
modules is an error at import; the same definition re-registering is not, because
Temporal's sandbox re-imports workflow modules to run them.

**Adding a durable capability does not mean editing the worker.** The one thing
still required is that the defining module be *imported* — the same
side-effect-import contract `chemclaw.agent.chemclaw_agent` has for tools, and why
the worker begins with a block of `# noqa: F401` imports.

**Heavy work is not here at all any more.** The xTB/CREST tasks are `CalcJobWorkflow`
on `connector-calc` (D-114) and BO is `BoCampaignWorkflow` on `connector-bo`. So a
capability that carries a dependency closure — `tblite`, `bofire` — carries it into its
own bundle and its own worker, and core's image holds none of it (D-118). D-006's
heavy/light split is intact: one core queue plus one per bundle, each sized for its own
work.

**Event history is not an archive** (`job_record.py`, D-157). A closed workflow's
history — and with it the result it returned — expires on the namespace's retention
clock, which no deployment here sets. So `connector_job.py` writes every finished
run to `job_records`: the launch arguments, the whole result envelope, and the
**reason it was asked for**, which is the one thing neither a note nor the audit
trail ever recorded. That row is what `get_durable_job_status` falls back to for an
old id, and what `find_past_jobs` searches.

Restarting a worker mid-job is the durability spike at CHECKMATE 1: the workflow
must resume from event history without re-running completed activities. For the
xTB jobs specifically, resumption is nearly free for a second reason — every
optimization and Hessian inside one is content-addressed in the calculation
store, so a retry walks straight through the work it already did.

## Module index

| Concern | Modules |
| --- | --- |
| The worker and its plumbing | `background_worker`, `serve` (run a worker so a pod termination drains rather than loses work), `registry`, `interceptor` (every activity says it ran and how it ended), `heartbeat`, `job_metrics`, `schedules` (the Temporal Schedules `make schedules-apply` writes) |
| Connector jobs | `connector_job`, `governed_launch` (a launch through the same governed chain a turn's goes through), `job_record` + `job_record_store`, `orchestrator` (child-workflow fan-out) |
| Ingest and indexes | `eln_sync`, `document_sync`, `corpus_sync`, `label_sync`, `commitment_sync`, `note_index` |
| Knowledge and memory | `publish` (the durable note write), `memory_jobs`, `observation_jobs`, `hypothesis_tournament` |
| Reports and templates | `report_workflow`, `template_job`, `template_activities` |
| Waiting on people | `awaiting` + `pending_store` (a held-open question), `orphaned_waits`, `check_in`, `notify` (session push-back), `digest`, `deliver_message` |
| Outbound and housekeeping | `publish_results` (drain the result outbox to enabled sinks), `effect_ledger`, `retention` (bounded growth for the durable stores), `artifact_eviction`, `eval_drift` |
