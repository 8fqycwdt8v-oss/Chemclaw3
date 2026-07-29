# `workflows/` — Temporal durable execution

**Responsibility:** the durable lifecycle of core's own long jobs (ELN sync,
re-index, reports, memory synthesis, approvals) and the one generic wrapper every
connector job runs inside (`connector_job.py`). Workflow code is deterministic and
replayable; all I/O and non-determinism lives in **activities**. Durability lives
here and **only** here, never in the MAF layer.

One core task queue (its name comes from `chemclaw.config`): `background-jobs`
(D-006). See `docs/reference/architektur.md` §2, §15.

**Which queue is a property of the capability, not of the deployment**, so it is
declared where the capability is defined rather than in a worker: put
`@durable_workflow("background")` above `@workflow.defn`, or
`@durable_activity("background")` above `@activity.defn`, and `workers/` serves it
(`registry.py`, D-099). A name claimed by two modules is an error at import; the
same definition re-registering is not, because Temporal's sandbox re-imports
workflow modules to run them.

**Heavy work is not here at all any more.** `hpc-jobs` held one workflow,
`QMJobWorkflow`, and it is a declared connector job now: the class kept its Temporal
type name (a rename would be a different command in a recorded history — see
`docs/guides/workflow-versioning.md`) and moved to `connectors/qm/workflows.py`, on the
bundle's own `connector-qm` queue. The xTB tasks went the same way earlier, as
`CalcJobWorkflow` on `connector-calc` (D-114). So a capability that carries a
dependency closure — the HPC bridge, `tblite`, `bofire` — carries it into its own
bundle and its own worker, and core's image holds none of it (D-118).
