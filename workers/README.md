# `workers/` — Temporal worker processes

**Responsibility:** the long-running processes that host workflow and activity
code and poll a task queue. Core has one now — `background-jobs`, the light work
(ELN sync, re-index, reports, and the `ConnectorJobWorkflow` wrapper). Workers are
thin: they connect using values from `chemclaw.config` and serve whatever
`workflows/` declared for their queue; they contain no business logic and no list
of their own.

**The heavy queue moved down a level.** `hpc-jobs` existed for the one capability
core still owned outright, the QM/DFT job. That job is a declared connector job now
(`connectors/qm/`), so it runs on `connector-qm` under the bundle's own worker
(`connectors/worker.py`), sized for HPC work in its own Helm entry. D-006's
heavy/light split is intact — it is just one core queue plus one per bundle, each
sized for its own work (D-118).

**Adding a durable capability does not mean editing a worker.** A workflow or
activity declares its queue where it is defined, with
`@durable_workflow("background")` / `@durable_activity(bundle_queue("qm"))`, and
`workflows.registry` is what these modules read (D-099). The one thing still
required is that the defining module be *imported* — that is the same
side-effect-import contract `agents.chemclaw_agent` has for tools, and it is why
this worker begins with a block of `# noqa: F401` imports.

Restarting a worker mid-job is the durability spike at CHECKMATE 1: the workflow
must resume from event history without re-running completed activities. For the
xTB jobs specifically, resumption is nearly free for a second reason — every
optimization and Hessian inside one is content-addressed in the calculation
store, so a retry walks straight through the work it already did.
