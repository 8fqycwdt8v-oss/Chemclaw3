# D-2026-10-21-a-template-may-map-and-select — templates gain a bounded fan-out step and a deterministic top-k step; until-converged loops stay in job specs

**Status:** proposed · **Date:** 2026-10-21

**Supersedes (in part, on acceptance):** [D-2026-08-25-the-loop-is-a-composite-not-a-template](D-2026-08-25-the-loop-is-a-composite-not-a-template.md), its placement of every fan-out in `connectors/calc/compose.py`. Its rule that a loop whose stopping depends on results lives in the job spec stands.

## Context

Templates sequence `tool`, `job` and `agent` steps and deliberately have no conditionals, loops or
expressions (`templates/manifest.py`). D-2026-08-25 put fan-out into Python composites.

The multi-fidelity funnel is the standard shape of computational screening, and it is pure
fan-out and selection:
1. screen N candidates cheaply;
2. keep the top k;
3. refine those at a dearer tier;
4. keep the top m.

Each funnel needs a new composite in code, and with D-2026-10-21-long-running-compute-runs-on-a-backend-seam
its refine stage becomes a durable child job, which no composite can own.
`durable/orchestrator.py::fan_out` already runs bounded, poison-tolerant child fan-outs for
memory and report jobs.

## Options

1. **Composites only** (status quo). Every funnel is Python in `connectors/calc/compose.py`.
2. **Bounded `map` and `select`.**
   - `map` runs one tool or job step over a referenced list, with `max_items` and `concurrency`.
   - `select` takes the top k by a field path, with optional `where` comparisons and no
     expressions.
3. **A general workflow language** (conditionals, loops, expressions). It turns templates into a
   programming language the plan gate and the run-ceiling check cannot reason about.

## Decision

**Option 2.**
- `map` runs through `fan_out`, with child ids suffixed by item index.
- The run ceiling is the per-item ceiling multiplied by ⌈items / concurrency⌉, checked before the
  run starts.
- An agent-authored workflow may `map` only read-only tool steps or jobs already allowed to
  composed workflows.
- Loops that stop on a result (BO, convergence) stay in job specs.

## Consequences

- These parts learn both kinds:
  - the template manifest, its resolver and its scheduler;
  - `durable/template_job.py`, behind a replay patch marker covered by `tests/test_workflow_replay.py`;
  - `template_step_ceilings()`;
  - `templates/composed.py::authored_problems`.
- A shipped `screen-then-refine.yaml` runs end to end on the mock backend.
- **On acceptance**, D-2026-08-25 gains `**Superseded-by:**` naming this ADR `(in part)`.
- Plan item: W13 Track A.

Revisit when: a shipped template needs a branch on a step's result. That is the line this decision
declines to cross, and crossing it is a new ADR.
