# D-2026-10-23-a-lost-tool-composite-is-recovered-by-asking-again — A tool composite whose enqueue failed gets no durable record; asking again republishes it

**Status:** accepted · **Date:** 2026-10-23 · **Owner decision** (2026-10-24, option 3)

## Context

A result reaches the outbox through one of three hooks (`src/chemclaw/publish/README.md`). The
cache and job hooks publish something already durable, so `publish/backfill.py` can walk
`calculation_results` and `job_records` and re-queue anything a failed enqueue missed. The tool
hook (`publish/hooks.py::publish_tool_result`) publishes `ThermochemistryResult` and `LogdResult`,
which are assembled in one turn and have neither a cache row (their key would name their own
output) nor a job record, so when that enqueue fails no walk in
`connectors/results/workflows.py::_walk` can find them again.

An enqueue fails in two ways, both already counted: the local write (the outbox is a table in the
deployment's own Postgres, `chemclaw_result_publish_failures_total`), or a projector that cannot
read the payload (a code gap, `chemclaw_result_projection_failures_total`). Both composites are
deterministic functions over cached primitives: `connectors/calc/compose.py::relax_to_minimum`
re-reads the optimisation and the Hessian from the cache ("a repeat costs round trips and no SCF"),
and `science/calc/logd.py` is Crippen plus one term over a cached remote pKa. Calling the tool
again produces the same payload and so the same content-addressed `calc_ref`
(`hooks._composite_ref`), and `outbox._ENQUEUE` makes that re-queue idempotent.

## Options

1. **A durable composite record and a walk.** A `tool_results` table (route, payload, arguments
   hash, tenant key) written beside every enqueue and walked by the backfill. Costs a migration, a
   retention rule, a tenancy key and a write on every composite call. It does not cover the more
   likely failure: the table lives in the same Postgres as the outbox, so the write that lost the
   enqueue loses this row too. It covers only the projector gap.
2. **Queue the raw payload when projection fails.** An outbox state for "not yet projectable",
   re-projected by a later release. Covers the projector gap without a new table, but puts rows the
   drain must skip into the drain's queue and changes the `result_publications` state check.
3. **Declare the loss and name the recovery.** No new record. The README's "What
   deliberately does not publish" names it, the two counters already make it visible, and the
   recovery is to call the tool again once the cause is fixed, which re-queues the identical
   record for round trips and no SCF.

## Decision

Decided by the owner 2026-10-24: option 3. Options 1 and 2 add a record whose only extra coverage
is the projector gap, and for these two composites asking again already recovers both failure
modes at no quantum-chemical cost.

## Consequences

`src/chemclaw/publish/README.md` lists the lost composite among the things that deliberately do not
publish, and the backlog row is deleted. A new member of `TOOL_COMPOSITES`, which
`tests/test_publish_reaches_the_hooks.py` derives, must also recompose from cached primitives, or
this decision has to be reopened for it.

Revisit when: a model is added to `publish/hooks.py::TOOL_COMPOSITES` whose composition runs a
calculation that is not served from the calculation cache, so asking again costs compute.
