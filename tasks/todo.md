# The three deferred items — plan approved 2026-08-27

Step 0 probed the environment's credential: **live** (the 2026-08-25 401 is gone), so Track A
takes the live-measurement path.

## Track B — job↔plan-step linkage (this repo)

- [x] ADR `D-2026-08-27-a-job-names-the-step-it-serves` + ledger row
- [x] `core/plan_context.py` — ambient `(plan_step, plan_hash)`, the `session_context` pattern
- [x] `agent/plan_link.py` — `stamp_plan_link` middleware (first `in_progress` todo +
      `plan_identity`), attached in `langgraph_agent.tool_governance_middleware` whenever the
      harness runs (innermost, inside the gate)
- [x] `ConnectorJobInput`/`JobRecord`/`JobRecordSummary` gain `plan_step`(+`plan_hash`); store
      columns + migration `057_job_plan_step.sql` (additive, defaulted)
- [x] `JobSignal`/`record_job_started` fold the ambient step in; `JobStartedEvent.plan_step`;
      `graph_stream` maps it
- [x] Tests: `tests/test_plan_link.py` (7), launch stamp + empty stamp in
      `test_connector_jobs.py`, Postgres round-trip + listing in `test_job_record_postgres.py`
- [x] BACKLOG §3 row deleted in this change
- [ ] `make lint type test` green, PR, auto-merge

## Track B UI — plan checklist job chips (Chemclaw3_ui)

- [ ] `shared/events.ts` `JobStartedEvent.plan_step` + helpers builder default
- [ ] chatStore job feed carries `planStep`; `PlanItems` badges the matching row
      (spinner running, ✓/✕ on completion/failure via job_id join)
- [ ] Tests; typecheck/lint/vitest green; PR, auto-merge

## Track A — verifier opt-in + judge margin (this repo)

- [ ] Chart/runbook opt-in surface (commented `CHEMCLAW_VERIFIER_*` in values.yaml naming the
      startup probe + the reproducibility caveat) + values-prose pin in `test_helm_chart.py`
- [ ] `infra/live` margin measurement: re-roll flagged answers 3×, measure flip rate/margin near
      threshold 0.7
- [ ] Hysteresis band from the measured width (`verifier_review_band`, re-roll majority inside the
      band only) + `chemclaw_verifier_band_rerolls_total`; ADR; delete the DEFERRED
      reproducibility row in the same commit
- [ ] Correct the stale BACKLOG §5 "API-KEY is present and rejected" row (re-measured live
      2026-08-27)

## Track C — trajectory census instrument (this repo)

- [ ] ADR defining "recurring trajectory" + the trigger numbers that would greenlight the
      distillation generator (which stays unbuilt until a real corpus exists)
- [ ] `chemclaw.cli.trajectory_census` + `make trajectory-census`; offline tests with fixture rows
- [ ] Delete the duplicate "Memory records…" BACKLOG row; point the surviving row at the
      instrument

## Deliberately not done (user-confirmed 2026-08-27)

- Code defaults for `harness_enabled`/`verifier_enabled` stay `False` — the chart is the opt-in
  surface (harness already on there).
- No explicit `plan_step` tool argument — rejected in the ADR with its reopen condition.
- No distillation generator, no synthetic corpus — the ADR defines the greenlight numbers.
