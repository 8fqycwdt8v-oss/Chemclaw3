# D-079 — Workflow versioning is a deploy checklist, not a CI guard

**Context.** Temporal replays workflow code against recorded history, so a control-flow change
deployed while a run is in flight fails that run with a nondeterminism error — surfacing after the
fact, on an unattended workflow, pointing at the new code rather than at the deploy. The 2026-07
campaign changed workflow logic (fan_out's local activity, `ElnSyncWorkflow`'s chunk loop, BO
activity seed args) with no `workflow.patched()` gates, which is safe only because no live cluster
holds Chemclaw histories yet. That safety expires at the first production deploy.

**Decision.** `docs/workflow-versioning.md` states the policy: what counts as a logic change (the
replayed command stream — activity/child calls, their arguments, type names, timers, loop bounds
and branch conditions) versus what does not (activity *bodies*, docstrings, logging, code no
workflow calls); the two sanctioned responses (`workflow.patched()` with a stable id and a planned
`deprecate_patch` retirement, or pausing the Schedules and draining in-flight runs as an explicit
deploy step); and a checklist for the release ticket. Cross-linked from `deploy/README.md` and the
runbook. Today's un-gated changes need **no retroactive patches** — gating them would add permanent
branches for a case that cannot occur without histories.

**Consequence, already applied.** The deferred `QMJobWorkflow` → `CalculationWorkflow` rename is
**dropped**, not deferred: a workflow type name is part of history, so renaming a class in place is
exactly the change this policy forbids — a cosmetic gain for a migration window.

**No CI guard, deliberately.** A check that fails a PR touching `workflows/*.py` without a
`workflow.patched()` call cannot distinguish a docstring edit from a reordered activity call, so it
would fire on nearly every PR; a check that is wrong most of the time trains its own bypass and
takes the real signal with it. `InteractionApprovalWorkflow`'s 7-day human hold is the concrete
reason draining is not always available, so the patch path stays the default. Revisit only if a real
incident shows the checklist being skipped.
