# D-114 — Sixth reconciliation with `main`: the xTB layer meets the connector seam

Two branches solved the same problem in the same window without knowing it. `main`'s X8 moved the
seven calculators out of the agent's process behind an MCP server because "the calculators carry the
heavy half of this system's dependency closure"; the connector seam (D-110) built the general
mechanism for exactly that. The merge is where they become one thing, and the interesting part is
what the merge *exposed* rather than what it moved.

**Convergent evidence, worth stating.** X8's reasoning and D-110's are nearly word-for-word — the
capability scales on its own pod, judgment stays out, only DTOs cross. Two independent derivations
of the same boundary is the strongest argument either has, and it settles the "is this seam the
right shape" question better than another round of design would.

**What was duplicated, and how the merge chose.** `mcp_servers/calc/server.py` and
`connectors/calc/server/tools.py` both defined `predict_pka`, `predict_solubility` and
`compute_xtb_energy` — two live definitions of one tool, differing in one place (X11's base-pKa
support, which the connector's copy lacked). The bundle is the surviving home and took `main`'s
better bodies plus its four newer calculators. `mcp_servers/calc/` is deleted. `mcp_servers/molfp`
and `mcp_servers/rxnfp` stay as the implementation modules their bundles wrap — those are one
capability with one definition, which is not the defect this was.

**The defect the merge exposed, and the reason this ADR is not just a merge note.** Five tools —
`compute_reaction_energy`, `compare_solvents`, `scan_coordinate`, `sample_conformers`,
`compute_interaction_energy` — stayed in-process on `main` with an explicit and well-argued
justification: they submit durable jobs, submitting needs `require_actor()` and
`get_current_session_id()`, and those are ambient to the turn and never model-supplied (F4-T3). The
argument is correct. Its conclusion was not, and after the merge the cost was visible: because they
route by *predicted* cost, they import `calc.xtb_cost`, `calc.reaction`, `calc.complexes` and
`calc.conformers` — so the chat service's image still loaded the entire heavy chemistry closure, and
the `calc` connector saved nothing it was built to save. The merge also left them **orphaned**: no
module imported them any more, so five capabilities were silently absent from the agent, caught by
`make skill-validate` rather than by anything at run time.

**The fix, and why it is better than what it replaced.** A new `JobSpec.inline_wait_seconds`: the
generated launcher starts the durable run and waits a bounded moment for it, returning the result if
it arrives and a job id if it does not. Identity never leaves core — the launcher is core's, running
in the turn, exactly as before. The capability never leaves the connector. One model-facing tool
serves both the two-second case and the twenty-minute one.

That it *replaces a prediction with a measurement* is the part worth keeping. A cost model is a
second model of the calculation and can be wrong in both directions: a mispredicted "cheap" call
blocks the turn anyway, and a mispredicted "expensive" one is deferred for nothing. Elapsed time
needs no model and cannot be wrong. And a prediction can only live where the cost model lives, which
is what had put chemistry in core in the first place — so the simpler mechanism is also the one that
removes the coupling. The wait is cancel-safe by construction: `asyncio.wait_for` cancels the waiter,
never the workflow, so an abandoned turn leaves a run that still completes, still caches and still
pushes back.

All five share one workflow. `XtbJobSpec` was already a closed union discriminated on `kind`, so each
job references its own member as `params_model` and `CalcJobWorkflow` dispatches — one durable path,
five separately-documented tools, because "compare these solvents" and "scan this bond" are different
questions even when the machinery is identical.

**Three consequences, none of them silent.**

1. **`run_xtb_task` is deleted.** X7's expert escape hatch took the raw union; the five typed jobs
   now cover that union exactly, so it had become a sixth tool doing what the five do, chosen by the
   model. Its role gate did not vanish with it: it existed for *unbounded* calculations, so it moved
   onto the two CREST searches (`sample_conformers`, `compute_interaction_energy`) as
   `expensive: true`. Dropping a gate along with the tool it guarded is how a posture loosens
   quietly.
2. **`get_job_status` narrowed to HPC/DFT, and `get_durable_job_status` grew a result.** The former
   dispatched on an id prefix over two kinds; one of those kinds no longer exists. The latter used
   to return a bare status word, which left a chemist holding a completed connector job with no tool
   that could fetch it — the connector envelope made that answerable, so it now reports the summary
   and the structured result in the same call.
3. **`main`'s `workflows/registry.py` is adopted, and a connector stays out of it.** The declarative
   `@durable_workflow(queue)` seam fixes a real failure — a workflow written, tested and imported but
   missing from a worker's list never runs — and core's two workers now assemble from it.
   A *connector's* workflows are deliberately not registered there: that registry serves core's
   queues, and a bundle polling its own queue on its own worker is the whole point. The test asserts
   the absence rather than the presence, because a connector workflow drifting back onto a core queue
   is the regression that would quietly restore the coupling this removed.

**ADR renumbering.** The branch's four ADRs were written as D-092…D-095 while `main` independently
used those numbers. They are D-110…D-113 here, with every in-repo reference updated. Numbering
collisions are the predictable cost of an append-only log on two branches; the alternative (a
reservation) is worse than renaming on merge.

**Two production gaps closed while reviewing, both of the same kind — a gate that existed but was
not wired.** CI ran `make skill-validate` and neither `connector-validate`, `template-validate` nor
`prose-validate`, so three of the five gates the seam added were enforceable only by hand; they are
CI steps now. And the image never `COPY`d `templates/` or `profiles/` (D-113) — both discovered from
disk, so the container would have started clean and simply offered fewer capabilities. A validator
nobody runs and a directory nobody ships fail the same way: silently, in the direction of less.
