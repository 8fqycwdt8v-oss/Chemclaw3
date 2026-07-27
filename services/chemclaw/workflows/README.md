# `workflows/` — Temporal durable execution

**Responsibility:** the durable lifecycle of long, expensive jobs — QM/DFT on
HPC, and light background jobs (ELN sync, re-index, reports). Workflow code is
deterministic and replayable; all I/O and non-determinism lives in **activities**
(submit, poll with heartbeat, parse). Durability lives here and **only** here,
never in the MAF layer.

Two task queues (names come from `chemclaw.config`): `hpc-jobs` for the few heavy
workers, `background-jobs` for light ones (D-006). See `docs/architektur.md`
§2, §15.

**Which queue is a property of the capability, not of the deployment**, so it is
declared here rather than in a worker: put `@durable_workflow("hpc")` above
`@workflow.defn`, or `@durable_activity("background")` above `@activity.defn`, and
`workers/` serves it (`registry.py`, D-099). A name claimed by two modules is an
error at import; the same definition re-registering is not, because Temporal's
sandbox re-imports workflow modules to run them.

**What belongs on `hpc-jobs`:** anything whose cost is measured in minutes.
`QMJobWorkflow` (HPC/DFT) and `XtbJobWorkflow` — the xTB tasks that
`calc.xtb_cost` predicts are too slow for a conversation turn, which on the
200-800 Da substrates this system targets is most of them (D-098, D-100).
