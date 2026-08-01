# D-2026-08-01-a-declaration-that-authorizes-nothing — A declaration that authorizes nothing

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** D-167 (the plan gate's single-turn
residual), D-118 (the connector bundle owns its jobs) · **Implements:** the full-codebase review's
`expensive: true` and plan-approval findings

## Context

**A manifest declaration granted nothing.** A job marked `expensive: true` in its `connector.yaml`
was routed through `authorize_trigger`, which returned immediately unless an operator *separately*
named that job in `entra_expensive_actions`. Reproduced under `entra_required=True` with a role-less
actor: `start_optimization_campaign`, `sample_conformers` and `compute_interaction_energy` were all
ALLOWED. Only `compute_dft_energy` was refused, and only because it happened to sit in the
hand-maintained `DEFAULT_WRITE_TOOL_GATES`.

Three places asserted the opposite — `runbook.md`, `connectors/manifest.py`'s `JobSpec`, and two
copies of a comment in `connectors/calc/connector.yaml` claiming an inherited `run_xtb_task` gate
that had been dropped.

**Deriving the declarations into the gate would still have changed nothing.** `_has_required_role`
treats an empty required set as satisfied — right for an operator's own gate, and here it meant the
derived set was allowed for everyone. The shipped Helm values set `entra_required=true` with both
role settings empty, so **the vulnerable shape was the shipped shape**, and no config validation
could catch it: a declared job needs no entry in either setting.

**A spent plan approval re-armed after an eviction.** The `chemclaw_plans_consumed` marker lived in
non-durable `session.state` while the `plan_approvals` row was durable in Postgres, so an LRU
eviction or a pod roll dropped the marker and kept the approval. `plan_approval_store.py` asserted
"under postgres both are durable", which was false. It composed with a second defect —
`current_plan_hash([])` is the global constant `4f53cda18c2baa0c` — so a *spent empty-plan* approval
re-armed with no human act at all.

## Decision

**The manifest declaration is the gate's source.** `expensive_actions()` unions
`settings.entra_expensive_action_set` with every `expensive: true` job across the enabled bundles,
exactly as `side_effecting_tools()` already derives the dry-run and plan gates. A test cross-checks
every manifest against the effective set, so a new bundle cannot regress it.

**An empty privileged role set fails closed** for this gate, matching the rule `authorize_tool`
already states for the built-in write gates.

**Config validation becomes asymmetric, and the asymmetry is the point.** Actions without roles is
an error — naming a gated action with no role that can pass it refuses it to everyone silently.
Roles without actions is the *normal* production setup, because the action set now derives from the
manifests; requiring the pair would force back the hand-maintained job list the derivation removed.
The previous `!=` symmetry check rejected exactly the configuration the runbook now instructs.

**Consumption is durable, in the same table as the approval.** Migration 034 adds `consumed_at` to
`plan_approvals`; `consume()` stamps it and the latest-decision query reads
`approved AND consumed_at IS NULL`. NULL means unspent, so an upgrade re-arms rather than silently
revoking. `consume_plan`, `plan_consumed` and `rearm_plan` are deleted from `harness_mode` —
`rearm_plan` needs no replacement, because the table is append-only and latest-wins, so recording a
fresh decision *is* the re-arm.

**A session proposing no work has no approvable identity.** `approvable_plan_hash` returns `None`
rather than the empty constant, and the decision route, the CLI and the display route all refuse it.
`todo_plan_items` strips `awaiting-job:` bookkeeping rows, so a session can show a non-empty plan and
still have nothing to approve.

**The chart declares the role setting as an explicit empty string rather than omitting it.** An
absent key appears in neither `helm show values`, nor the rendered ConfigMap, nor an operator's
values diff.

## Consequences

- **A behaviour change operators will see.** On the shipped chart, declared-expensive jobs go from
  running for any authenticated user to refusing everyone until `CHEMCLAW_ENTRA_PRIVILEGED_ROLES`
  is set. That is the intended direction, and `deploy/README.md` now has a section naming the four
  jobs it closes.
- `SECURITY.md` and the `authz.py` comment are corrected: the manifest is the source, and an empty
  role set refuses rather than admits.

## Alternatives rejected

- **Ship a placeholder role name in `values.yaml`.** The worse option, and the reason is the failure
  mode: a placeholder ships a config that *looks configured*, survives review, reaches the cluster
  and grants nothing — and the resulting refusal ("the account holds none of the roles this tool
  requires") points the operator at Entra group membership rather than at `values.yaml`. The other
  placeholders in that block are safe because their *shape* is inert: a zero GUID fails a token
  exchange loudly. Every string is a valid role name, and the wrong one fails silently **by design**,
  because "you do not hold this role" is the gate working correctly. It would also be the chart
  asserting an organizational fact it cannot know.
- **Keep the consumed marker in session state and accept the eviction window.** The empty-plan fix
  closes the *composition*, but a model that reconstructs a byte-identical todo list after an
  eviction still gets its spent approval back — which is the GxP line this gate exists to hold.
- **A SQL CHECK or a separate consumption table.** The approval and its consumption are one fact
  about one decision; splitting them invents a join that can disagree with itself.
