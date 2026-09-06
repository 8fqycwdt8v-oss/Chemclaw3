# D-2026-09-06-the-write-gate-is-three-names-and-the-plan-gate-carries-the-rest — the RBAC fallback is three knowledge-graph writers, the plan gate covers the durable launchers, and both halves are now measured

**Status:** accepted · **Date:** 2026-09-06 · **Builds on:** D-068 and D-167 (the two gates, and why
they are separate), `D-2026-08-01-a-cap-that-starves-a-source` (measure it, don't argue it),
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` (the defect class this is an instance of)
· **Corrects prose in** `src/chemclaw/agent/authz.py` and `deploy/README.md`; changes no gate.

## Context

Two operator-facing sentences overstated what this system's authorization posture closes. Neither
was measured by anything, and both were found by driving the gates rather than reading them.

**`DEFAULT_WRITE_TOOL_GATES`' comment.** It ended: *"a tool that launches a job or mutates state
must never be callable by any authenticated user just because nobody remembered to gate it — writes
are closed by default, opened by explicit operator config."* The set holds three names. Driven under
the shipped chart posture (`CHEMCLAW_ENTRA_REQUIRED=true`, `CHEMCLAW_TOOL_AUTHZ_DEFAULT=allow`,
`CHEMCLAW_TOOL_ROLE_GATES` and `CHEMCLAW_ENTRA_PRIVILEGED_ROLES` empty) with an authenticated actor
holding **no roles**, over this checkout's enabled bundles: `authorize_tool` refused 3 of 49
side-effecting tools, `authorize_trigger` refused 17, and **29 passed both** — including every
`run_*` template launcher. Driven through the compiled graph rather than the predicate,
`run_conformer_refinement` reached its tool body and stopped only on argument validation.

**`deploy/README.md`'s blast-radius section.** It listed three jobs closed by the empty
`CHEMCLAW_ENTRA_PRIVILEGED_ROLES` and said "Nothing else breaks". Measured: seventeen, including
`request_development_report` and `synthesize_memory`, which are core-owned
(`CORE_EXPENSIVE_ACTIONS`) and appear in no bundle the table could have covered. That section exists
*because* the failure is silent — healthy pod, a capability shut — and it under-reported it 5.7x.

## Decision

**Change no gate. Change both claims, and put a measurement behind each.**

Widening `DEFAULT_WRITE_TOOL_GATES` to make its own sentence true was rejected, and the reason is the
one already recorded beside `STATE_CHANGING_TOOLS`: membership costs an unconfigured deployment
access to a tool it can call today, so a widening is a deployment-visible narrowing that arrives
with an upgrade rather than with an operator's decision. A control is not improved by being changed
to fit a docstring written about it.

What actually covers the 29 is the plan gate: the shipped chart sets `CHEMCLAW_HARNESS_ENABLED=true`
/ `CHEMCLAW_HARNESS_AUTONOMY=plan_only`, so a human approves the plan before any of them runs, and
that gate is applied to the *act* rather than latched onto the session (D-167). **The residual is
therefore stated rather than implied**, in the comment where a reader of the gate will meet it: with
the harness off, under `execute` autonomy, or for a call inside an already-approved plan, a role-less
authenticated user reaches a durable launcher with no RBAC underneath.

The README stops holding a list at all. Most of the expensive set is declared by bundles served out
of `Chemclaw3-mcp`, which this repository does not build and cannot watch, so any table here goes
stale on somebody else's merge — the `SERVED_ELSEWHERE` problem CLAUDE.md already records for the
context floor. It names a command instead, and the command is `expensive_actions()` itself.

## What keeps it true

Three tests, in `tests/test_authz.py`:

- `test_the_built_in_write_gate_closes_three_knowledge_writers` — the two RBAC gates driven over the
  live side-effecting surface under the chart posture, as **set** equalities rather than counts
  (the surface grows with every enabled bundle). Its load-bearing assertion is that every template
  launcher passes both gates: if that stops being true, the posture changed and the comment must.
- `test_the_plan_gate_refuses_a_launcher_the_rbac_gates_leave_open` — the covering control driven,
  not cited: a launcher called under an unapproved plan raises `PlanNotApprovedError` through the
  real middleware and the real approval store. "Something else refuses it" is exactly the kind of
  sentence this repository has shipped without a producer behind it.
- `test_the_operator_note_lists_no_expensive_job_by_name` — lifts the `uv run python -c` payload out
  of `deploy/README.md`, executes it, and compares its output with the live set. It catches both an
  operator instruction that no longer runs and a hand-list creeping back in.

## Consequences

No behaviour changes; two claims stop overstating a control. The residual is now written down where
the gate is, so a deployment turning the harness off knows what it is turning off, and a session
proposing to widen the RBAC fallback has the argument against it in one place rather than having to
rediscover the blast radius.

## What was measured rather than assumed

- 46/49 side-effecting tools open at `authorize_tool`, 17 refused at `authorize_trigger`, 29 passing
  both, with `run_conformer_refinement` reaching its tool body through the compiled graph.
- The three tests above watched to fail: the first against a `DEFAULT_WRITE_TOOL_GATES` widened by
  one launcher, the second against `enforce_plan_approval`'s gating predicate short-circuited, the
  third against the README's original table.
