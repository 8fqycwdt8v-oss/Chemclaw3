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
- [x] `make lint type test` green (4918 passed + 2 contract pins updated); **PR #249 merged** (squash 550b52f)

## Track B UI — plan checklist job chips (Chemclaw3_ui)

- [x] `shared/events.ts` `JobStartedEvent.plan_step` (+ eventContract full-fixture)
- [x] `planJobs.ts` derives step→status from trace AND jobFeed; `PlanItems` chips
- [x] 532 tests green; **PR #31 merged** (squash ae7493b) — after one prettier round CI caught
- [x] Chemclaw3_mock measured: holds NO copy of the event contract; the backend test's claim
      corrected in #249 rather than a phantom mirror updated

## Track A — verifier opt-in + judge margin (this repo)

- [x] Chart/runbook opt-in surface (commented `CHEMCLAW_VERIFIER_*`, runbook §(xvi-b)) +
      values-prose pin in `test_helm_chart.py`
- [x] Margin measured live: `make live-verifier-margin`, 24 pairs × 4 rolls on haiku — flip rate
      near threshold 6.25%/roll (validates the DEFERRED row's 5.1%), max dev-from-median 0.167,
      grounded class 0.000 over 32 rolls; artifact `docs/archive/verifier-margin-2026-08-27.json`
- [x] Band shipped from the measurement: `judge_once` extracted, `_banded_verdict` median-of-rolls
      inside `verifier_review_band` (default 0.2 = measured 0.167 rounded up),
      `chemclaw_verifier_band_rerolls_total` declared; ADR
      `D-2026-08-27-a-verdict-at-the-margin-is-a-coin-toss`; DEFERRED reproducibility row deleted
      and its sibling's dangling cross-reference corrected
- [x] BACKLOG §5 credential row corrected (re-measured live 2026-08-27; state, not fact)

## Track C — trajectory census instrument (this repo)

- [x] ADR `D-2026-08-27-count-the-trajectories-before-building-the-distiller` — definitions +
      greenlight trigger (≥5 classes, ≥3 sessions, ≥1 helped multi-tool), evaluated by the CLI
- [x] `chemclaw.cli.trajectory_census` + `make trajectory-census`; 7 offline tests; run live:
      0 sessions, 0 turns, not greenlit (the honest zero, now one command to re-check)
- [x] Duplicate "Memory records…" BACKLOG row deleted; surviving row names the instrument

## Deliberately not done (user-confirmed 2026-08-27)

- Code defaults for `harness_enabled`/`verifier_enabled` stay `False` — the chart is the opt-in
  surface (harness already on there).
- No explicit `plan_step` tool argument — rejected in the ADR with its reopen condition.
- No distillation generator, no synthetic corpus — the ADR defines the greenlight numbers.
