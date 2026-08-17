# D-2026-08-17-a-workflow-type-is-a-launch-contract-not-a-durability-leak — the agent may name a core-queue workflow type; it may never name a bundle's

**Status:** accepted · **Date:** 2026-08-17 · Refines D-002 by saying what it does *not* forbid.
Extends the guard `tests/test_layering.py::test_the_connector_job_wrapper_imports_no_connector`
(D-2026-08-05) to the other side of the same seam.

## Context

`agent/durable_tools.py` imports `DevelopmentReportWorkflow` and `NoteReindexWorkflow` in order to
launch them. D-002 puts durability in Temporal and orchestration in layer 1, so an agent-layer
module holding a workflow implementation reads as a layering break — the conversation layer reaching
into the durable one.

The suspicion is worth taking seriously because this repository has been wrong in this exact
direction before: `connectors/bo/worker.py` exists precisely so `bofire` and `botorch` load in one
process and nowhere else, and `durable/registry.py` states the mechanism — *"the isolation comes
from the import boundary, not from the decorator"*.

## What was measured

**The surface is four modules, not one.** `agent/durable_tools.py`, `agent/interaction_tools.py`,
`connectors/jobs.py` and `templates/registry.py` all import a workflow class to launch it. Whatever
is decided here is a rule about all four, not a fix to one file.

**1. The marginal closure is 10 modules and zero third-party packages.** Importing
`chemclaw.agent.durable_tools` loads 1,898 modules; importing everything it names *except* the two
workflow classes loads 1,888. The difference is `report_workflow`, `note_index` and eight chemclaw
modules the agent layer already carries for `gather_evidence` — `core.embeddings`, `core.fulltext`,
`kg.conflicts`, `kg.search`, `retrieval.retrievers`, `retrieval.vector_index`,
`science.fingerprints.rxnfp.search`. **No third-party root is pulled in at all.** D-115's claim that
the report's dependency closure "is what core keeps for `gather_evidence` anyway" was asserted in a
docstring and is now measured true.

**2. The agent process carries no bundle.** In a clean interpreter, `agent.durable_tools`,
`agent.langgraph_agent` and `api.app` each load **zero** modules from any of the seven discovered
bundles, and none of `bofire`, `botorch`, `tblite`, `gpytorch`, `xgboost`. The expensive half of the
durable layer is not reaching layer 1.

**3. The typed reference is load-bearing, and the alternative is silent.** Temporal offers a
by-name launch — `start_workflow("DevelopmentReportWorkflow", …)` — which is what removing the
import would require. Passing a wrong argument type both ways, under this repository's
`mypy --strict`:

| launch form | wrong argument |
|---|---|
| `start_workflow(DevelopmentReportWorkflow.run, "not-a-report-request", …)` | **error** — `Cannot infer value of type parameter "ParamType"` |
| `start_workflow("DevelopmentReportWorkflow", "not-a-report-request", …)` | **no error** |

The same loss is already visible where by-name dispatch is genuinely required:
`durable/connector_job.py` must restate `result_type=ConnectorJobResult` by hand, because naming the
child by string threw the type away.

**4. The direction is already two-way, and the other direction is the heavy one.**
`durable/template_activities.py` imports `agent.profiles`, `agent.state`, `agent.tool_invocation`,
`agent.langgraph_agent`, `agent.authz` and `agent.chemclaw_agent` — a durable activity runs a whole
agent turn. "Layer 1 must not know layer 2" was never the architecture; `agent → durable` is the
*lighter* half of a pair that already exists.

**5. The edge is declared, not hidden.** `tests/test_layering.py::_CYCLE_EDGES` carries
`("chemclaw.agent", "chemclaw.durable")` with the reason *"agent tools start durable jobs"*, and
each direction is checked independently.

## Decision

**The import stays, and D-002 is not what forbids it.** D-002's words are that merging both
*durability models* is avoided and that the integration is "one thin DIY adapter". The test is
whether a module **stores or owns durable state**, not whether it names a durable type.
`durable_tools.py` stores nothing: it authorizes, derives a deterministic id, calls
`start_workflow`, and returns. It *is* the thin adapter D-002 asks for — which its own module
docstring already claims, correctly.

So the rule this ADR states positively:

> A launcher may import the workflow type it launches **when that type's closure is already in the
> launching process**. When it is not — a connector bundle's workflow — the launch goes by name
> across the queue, and the type must not be imported at any cost.

That is not a compromise between the two forms; it is the reason each exists. The typed reference
buys argument checking at the one site where durable identity and D-011 idempotency are decided, and
costs nothing when the closure is shared. By-name dispatch buys process isolation and costs the type
— which is why it is confined to the boundary that actually needs it.

## What was actually missing

The concern points at a real gap, one layer below where it was aimed. The invariant that protects
the agent process is not "agent must not import durable" — it is **"the agent must never import a
bundle's workflow"**. That held (measurement 2) and **nothing asserted it**.

Core's *worker* has exactly this guard, `test_cores_workers_import_no_bundle`. The agent layer,
which also launches workflows, had none. `test_layering.py`'s policy cannot supply it: it is
package-granular, and `chemclaw.agent → chemclaw.durable` is legitimately allowed, so importing
`chemclaw.connectors.bo.workflows` straight into an agent tool would have passed every test in the
file. That is the identical hole that produced
`test_the_connector_job_wrapper_imports_no_connector` on the other side of the seam, found by the
2026-08-05 review — and it was open here the whole time.

Closed by `tests/test_layering.py::test_the_agent_layer_imports_no_bundle_workflow`: in a clean
interpreter, the agent's two launchers and the front door must load no bundle package and none of
the bundle-only heavy dependencies. Bundles are derived from `connectors.registry.discovered()`
rather than listed, so a bundle added tomorrow is covered on the day it is created.

A guard that only ever passes is worth little, so the test was confirmed to fail on the violation it
describes: adding `from chemclaw.connectors.bo.workflows import BOJobWorkflow` to
`agent/durable_tools.py` turns it red, naming both the bundle module and the heavy dependency
(`bofire`, `botorch`) that arrived with it.

## Consequences

- No change to how any of the four launchers start a workflow. The investigation's conclusion is
  that the shape is correct, so editing it would have been churn.
- One comment at `durable_tools.py`'s import site records why the import is allowed, because this
  investigation shows a reader arrives at that line with the question.
- The rule is now enforced on both sides of the connector seam: core's worker imports no bundle,
  and neither does the agent process.
- If a future durable capability's closure is *not* already in the agent process and it is not a
  bundle either, that is the signal it should be a bundle — the third case does not need a third
  mechanism.
