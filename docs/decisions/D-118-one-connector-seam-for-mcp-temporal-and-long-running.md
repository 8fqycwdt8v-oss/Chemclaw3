# D-118 — One connector seam for MCP, Temporal and long-running HPC tools

The seam D-110 built covers plain MCP tools and connector-owned durable jobs declaratively. It does
not cover the third kind — a long-running HPC job — which is still four hand-written core edits per
capability, and it carries three smaller duplications beside it. This ADR closes them. It opens with
the defect that shaped the rest, because that defect was invisible and is the reason the seam needs
a *mechanical* boundary rather than a documented one.

**The chat service was loading the quantum-chemistry closure the `calc` bundle exists to exclude.**

`connector.yaml`'s `params_model` names a pydantic model as `module:Class`, and
`connectors/jobs.py` resolves that name by **importing** it — inside `build_job_tool`, which
`agents/chemclaw_agent.py` calls on *every* `build_agent`, and again in `make connector-validate`.
The `calc` bundle pointed its five jobs at `workflows/models.py`, which imported `calc.complexes`,
`calc.conformers`, `calc.reaction` and `calc.xtb_scan` for the *result* types that happened to live
in the same file as the *request* types.

Measured from a clean interpreter, building the enabled job tools loaded:

```
heavy third-party: ['tblite', 'tblite._libtblite', 'tblite.exceptions',
                    'tblite.interface', 'tblite.library']
calc.* modules:    15  (anc, complexes, conformers, crest_cli, progress, reaction, store,
                        structure, xtb_cli, xtb_engine, xtb_opt, xtb_scan, xtb_spec, xtb_thermo)
```

`tblite` is a compiled quantum-chemistry library. It was resident in the chat pod, which never calls
it. Nothing failed and no test noticed, because the coupling arrives through a *string in YAML*
rather than through an import statement anyone would read. This is exactly the closure
`connectors/calc/workflows.py` says D-114 removed — *"which is what kept the whole heavy chemistry
closure inside the chat service's image"* — quietly restored by the one field that resolves an
import.

**The fix is a split on what a module may import, not on what it is about.** Requests move to
`connectors/calc/specs.py`, a leaf importing pydantic and config only; results move to
`connectors/calc/results.py`, which may import the heavy `calc.*` types because only this bundle's
own worker ever imports *it*. After the move the same measurement reports `heavy: NONE`,
`calc.*: NONE`.

`tests/test_connector_isolation.py` asserts it in a **subprocess**, which is not incidental: by the
time any test runs, the session's `sys.modules` already holds everything every other test imported,
so an in-process check would pass no matter what the manifest said. Counterfactually verified — a
single heavy import added back to the leaf module fails it with the exact five `tblite` entries.

**`JobStatus.xtb_result` went with it, and was already dead.** The field, and the
`kind: Literal["qm", "xtb"]` that chose between it and `qm_result`, date from when one status tool
answered for both engines. D-114 moved the xTB job into the `calc` bundle, where
`get_durable_job_status` reports it through the connector envelope, so `agents/job_status.py` has
hardcoded `kind="qm"` and populated only `qm_result` ever since. Nothing wrote or read
`xtb_result` — grep confirms a single occurrence in the whole tree, its own declaration. Removed
rather than kept as a field whose `None` means "unreachable here"; it was also the last thing
pulling the `calc.*` result closure into core.

**A bundle's isolation comes from the import boundary, not from withholding a decorator.**

`connectors/bo/activities.py` carried this rationale for leaving its activities undecorated:
*"registering them there would put `bofire` and `botorch` back into core's background worker, which
is exactly the coupling the bundle removed."* The conclusion was right and the mechanism was
backwards. `workflows/registry.py`'s dicts are populated at **import** time, and core's workers
import only `workflows.*` — so a decorator on a module core never imports cannot move anything into
core. What kept `bofire` out was the missing import, not the missing decorator.

The cost of getting the mechanism wrong was real: each bundle had to hand-maintain `TASK_QUEUE`,
`_WORKFLOWS` and `_ACTIVITIES` in its worker module, which re-created one level down the exact
failure the registry exists to prevent — *a workflow that is written, tested and imported but
missing from the worker's list never runs, and nothing fails until someone submits one and it waits
in the queue forever*. And the queue name had three copies that all had to agree (the manifest, the
worker constant, the Helm component); two that disagree is a job in a queue nobody polls.

So: `Queue` widens from a two-member `Literal` to a string, because a bundle must be able to name
its queue without editing core. `connectors/queues.py::bundle_queue` derives it from the bundle
name, so the three copies become one derivation. Bundles decorate normally, and each worker module
is now five lines — two registration imports and a call — with `connectors/worker.py` holding the
shared body. That extraction is sanctioned by the file it replaces: `bo/worker.py` said *"the second
connector worker is when to look at it again"*, and `calc` is the second.

D-006's queue split therefore moves down one level: from core's two queues to one core queue plus
one per bundle, each sized for its own work in Helm.

`tests/test_workflow_registry.py` swaps an assertion about a decorator's absence for the property
that was actually doing the work — in a fresh interpreter, importing core's workers must load no
bundle package and none of `tblite`/`bofire`/`botorch`. The bundle list is derived from
`connectors.registry.discovered()`, so adding a bundle extends the check on the day it is created.

Verified live against the dev server — both workers connect and serve exactly what the registry
holds, with nothing hand-listed:

```
bo   connector worker connected: queue=connector-bo   workflows=[BoCampaignWorkflow]
     activities=[evaluate_candidates, propose_initial, propose_next]
calc connector worker connected: queue=connector-calc workflows=[CalcJobWorkflow]
     activities=[run_xtb_calculation]
```

**The HPC job is a declared connector job, and core's `hpc-jobs` queue is gone with it.**

That was the third kind this ADR opened with, and it cost four hand-written core edits per
capability: a launcher tool (`agents/qm_tools.py`), a status tool that knew the job's own result
shape and id prefix (`agents/job_status.py`), a queue (`hpc_task_queue`), and a worker
(`workers/hpc_worker.py`). `connectors/qm/` replaces all four with a manifest. The move is
mechanical because the earlier commits made it so — `bundle_queue("qm")` derives the queue, a
five-line `worker.py` serves whatever the imports registered, and `specs.py`/the rest is the leaf
split `calc` established.

**The class is not renamed, and that is deliberate.** `@workflow.defn` derives the Temporal type
name from `__name__`, so a *module* move is invisible to a recorded history while a *class* rename
is a different command in it. `docs/workflow-versioning.md` already records the
`QMJobWorkflow` → `CalculationWorkflow` rename as dropped rather than deferred, for exactly this
reason; `QMJobWorkflow` therefore keeps its name in its new home.

**What the workflow stopped doing is the substance of the change.** It published its own graph note
and sent its own session push-back. Both are obligations `ConnectorJobWorkflow` — now its parent —
already owns for every other bundle, so the note is *built* in `connectors/qm/knowledge.py` and
returned on `ConnectorJobResult.note`, and `write_knowledge_node` (the activity that called
`propose_note` directly) is deleted. `connectors/qm/knowledge.py` no longer imports `kg.pr_gate` at
all: a connector reaching around the GxP gate is now structurally impossible rather than merely
against the rules, which is the same correction `connectors/bo/knowledge.py` took in D-111.

**`requested_by` travels on the run's memo.** The HPC cluster is submitted to under a shared service
identity, so the requesting user is the only thing that makes a run attributable (F4-T3), and it
must reach `submit_to_hpc`. It cannot ride on the spec: `params_model` becomes the JSON schema the
model fills in, so a `requested_by` field there would be one an LLM could author. So
`ConnectorJobWorkflow` passes `memo={"requested_by": job.requested_by}` on `execute_child_workflow`
and the bundle reads `workflow.memo_value(...)` — per-execution metadata beside the argument, not
inside it. `QmJobSpec` is the three scientific fields and nothing else; `QMJobInput` subclasses it
with the actor, so the two cannot drift.

**`get_durable_job_status` is now the only way a finished job is collected**, and its
`ValidationError` fallback became a hard error. That branch existed for one job — the QM run, whose
bespoke result the generic tool could not read — and every launcher in the system returns the
envelope now. Reporting `completed` with an empty result for a week-long calculation is worse than
raising.

Two things could not be done as specified, and one gap was found in passing. `JobSpec` has no
`timeout_seconds` field, so the manifest declares none: a connector job's ceiling is the global
`connector_job_timeout_seconds` (24 h), which the field's own comment defends as "a bundle in the
repo must not be able to grant itself unlimited runtime". A DFT run that legitimately needs a week
therefore needs that number raised at the deployment, not in `connector.yaml`. And the harness's
awaiting-todo bridge (`mark_awaiting_job`, D-040) turned out to have exactly one caller — the QM
launcher — so it had never applied to any other durable job; it moved into `connectors/jobs.py`,
where it now covers all of them.

Verified live against the dev server, end to end through the generated tool:

```
qm connector worker connected: queue=connector-qm workflows=[QMJobWorkflow]
   activities=[parse_qm_output, poll_hpc_status, prepare_input, submit_to_hpc]

launched: qm-compute_dft_energy-29776d63ecaa48fb
status:   completed
summary:  B3LYP/def2-SVP on CCO: -94.100000 Hartree (converged)
result:   {... 'requested_by': 'oid-live-check'}
```
