# D-2026-10-21-compute-is-budgeted-beside-tokens — compute spend is reserved before a job starts and settled from what the backend reports, under per-actor and per-project limits

**Status:** proposed · **Date:** 2026-10-21

## Context

`spend/` bounds model tokens and turns, per turn, session and actor (`SpendPolicy`, `budget_usage`,
`turn_costs`). Compute is bounded only by:
- the `expensive` gate, which asks *who* may launch, not how much;
- per-pod admission ceilings;
- job ceilings granted by an operator.

The only compute figure recorded is `job_records.runtime_seconds`. A cluster backend
(D-2026-10-21-long-running-compute-runs-on-a-backend-seam) charges allocations in core-hours or
GPU-hours. Nothing would stop one chemist's campaign from spending a project's allocation, and the
agent cannot see a cost before it launches.

## Options

1. **Admission ceilings only** (status quo). Concurrency is bounded and spend is not.
2. **A compute ledger.** It has these parts:
   - `ComputeUsage{backend, unit, amount}`.
   - A `compute_usage` table with reserved and used amounts per actor, project and window.
   - `ComputePolicy` limits.
   - A reservation at launch from the driver's `estimate`, settlement from `BackendStatus.usage`,
     and release on failure or cancellation.
3. **Leave accounting to the cluster's scheduler.** It sees service-account jobs, not chemists or
   projects. Its refusal would arrive as a failed job, after the plan was approved.

## Decision

**Option 2**, inside the single `spend/` module that W5.6 creates.
- A launch over budget is refused before submission, and the refusal names the remaining balance.
- The `fleet` backend settles CPU-seconds, so xTB work is visible on the same ledger.

## Consequences

- `compute_usage` is created with `tenant_id` and RLS.
- An on-demand `compute_budget_status` tool, together with estimates in the job launchers'
  docstrings, lets the agent reason about cost before asking for approval.
- `operations/` and the evidence pack report compute spend beside tokens.
- Plan item: W13 Track C. Depends on W5.6.
