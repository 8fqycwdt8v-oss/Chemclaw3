# Task — `agent/durable_tools.py` imports workflow implementations: is that a D-002 layering break?

Raised as: "agent/durable_tools.py imports workflow implementations in order to launch them, where
D-002 puts durability only in Temporal. That's a genuine layering question and wants its own ADR,
not a readability edit."

## Plan

- [x] Establish the real surface: which modules outside `durable/` import a workflow class
- [x] Read what D-002 actually says (vs. what the concern assumes it says)
- [x] Measure the marginal import closure the workflow-class imports add to the agent process
- [x] Measure whether the agent/front-door process currently loads bundle-only heavy deps
- [x] Measure what the by-name alternative costs under `mypy --strict`
- [x] Check whether the edge is already declared/enforced anywhere
- [x] Decide, write the ADR, add the ledger row
- [x] Close the one gap the investigation actually found, with a test
- [x] `make lint type test` green

## What was measured

| Question | Result |
|---|---|
| Launch sites outside `durable/` | **4**, not 1 (`agent/durable_tools`, `agent/interaction_tools`, `connectors/jobs`, `templates/registry`) |
| Marginal closure of the 2 workflow imports in `durable_tools` | **10 modules, 0 new third-party roots** (1898 → 1888) |
| Bundle-only heavy deps in agent / front door | **none** (`bofire`, `botorch`, `tblite`, `gpytorch`, `xgboost`) |
| Bundle modules loaded by agent / front door | **zero**, across 7 discovered bundles |
| Wrong workflow argument, typed launch | `mypy --strict` **errors** |
| Wrong workflow argument, by-name launch | **silent** |
| Dependency direction | already **bidirectional** — `durable/template_activities.py` imports `chemclaw.agent` to run a turn inside an activity |
| Edge already declared? | **yes** — `tests/test_layering.py::_CYCLE_EDGES` has `("chemclaw.agent", "chemclaw.durable")` with its reason |

## Review

**The premise does not hold, and the measurement is what settles it.** D-002 forbids merging
*durability models* — a second durable store in the conversation layer — and asks for the
integration to be "one thin DIY adapter". `durable_tools.py` is that adapter: it stores nothing
durable, and the import is Temporal's own typed launch API. Removing it would trade a compile-time
error for a runtime one at precisely the site where durable identity and D-011 idempotency are
decided.

**What the concern does correctly point at is one layer down, and it was real:** the rule that
actually protects the agent process is not "agent must not import durable" — it is *"the agent must
never import a **bundle's** workflow"*, which is what the connector seam's by-name cross-queue
dispatch exists for. That held (measured above) and **nothing asserted it**. Core's *worker* has
exactly that guard (`test_cores_workers_import_no_bundle`); the agent layer, which also launches
workflows, had none — and `test_layering.py`'s policy is package-granular, so it structurally
cannot express it. The same gap, on the other side of the seam, is why
`test_the_connector_job_wrapper_imports_no_connector` was written.

Closed by `tests/test_layering.py::test_the_agent_layer_imports_no_bundle_workflow`. ADR:
`D-2026-08-17-a-workflow-type-is-a-launch-contract-not-a-durability-leak`.

**Not done:** no change to `durable_tools.py`'s launch shape — the investigation concluded the
import is correct, so editing it would have been the readability edit the task explicitly excluded.
One comment was added at the import site, because "why is this allowed?" is a question this
investigation shows a reader will have again.
