# `workers/` — Temporal worker processes

**Responsibility:** the long-running processes that host workflow and activity
code and poll a task queue. One worker fleet per queue: heavy workers on
`hpc-jobs` (QM/DFT and the expensive xTB jobs), light workers on
`background-jobs`. Workers are thin — they connect using values from
`chemclaw.config` and serve whatever `workflows/` declared for their queue; they
contain no business logic and no list of their own.

**Adding a durable capability does not mean editing a worker.** A workflow or
activity declares its queue where it is defined, with
`@durable_workflow("hpc")` / `@durable_activity("background")`, and
`workflows.registry` is what these modules read (D-086). The one thing still
required is that the defining module be *imported* — that is the same
side-effect-import contract `agents.chemclaw_agent` has for tools, and it is why
each worker begins with a block of `# noqa: F401` imports.

Restarting a worker mid-job is the durability spike at CHECKMATE 1: the workflow
must resume from event history without re-running completed activities. For the
xTB jobs specifically, resumption is nearly free for a second reason — every
optimization and Hessian inside one is content-addressed in the calculation
store, so a retry walks straight through the work it already did.
