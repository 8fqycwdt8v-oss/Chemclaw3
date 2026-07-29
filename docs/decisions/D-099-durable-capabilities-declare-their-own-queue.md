# D-099 — Durable capabilities declare their own queue

**Context.** Adding `XtbJobWorkflow` (D-098) meant editing a hardcoded list inside
`workers/hpc_worker.py`. That was the *one* extension seam left in the system that forced an
edit to infrastructure code: agent tools declare themselves with `@tool`, metrics with
`@metric`, skills by folder, MCP servers and data sources by config token — and workflows by
being remembered. The failure is silent and total: a workflow that is written, tested and
imported but missing from a worker's list never runs, and nothing fails until a job sits in the
queue forever.

**Decision.** `workflows/registry.py`, shaped exactly like `agents.tool_registry`: a
`@durable_workflow("hpc")` / `@durable_activity("background")` decorator at the definition site,
a dict per queue keyed by the name Temporal will advertise, insertion-ordered, with a duplicate
guard. Both workers now read what they serve from the registry instead of restating it, and the
startup log line is derived from it too, so it cannot go stale.

**The queue is a property of the capability, not of the deployment** (D-006): `hpc` for few
heavy workers, `background` for many light ones. Which one a durable job belongs on follows from
what it does, so the declaration belongs next to the code that does it.

**Two details the shape forced.** The key is the *Temporal* name, read from the definition
Temporal attached, not the Python name — the registry's job is catching two capabilities
claiming one name, so it has to key on the name that actually collides. And re-registering the
**same** definition is allowed, because Temporal's workflow sandbox re-imports workflow modules
to run them and would otherwise trip the guard on every workflow task; the guard compares the
defining module rather than object identity.

**What still requires an edit,** and honestly: a workflow in a *new* module needs one import
line in the worker, because importing is what triggers registration. That is the same
side-effect-import contract `agents.chemclaw_agent` has for tools, and it is one line rather
than two lists.
