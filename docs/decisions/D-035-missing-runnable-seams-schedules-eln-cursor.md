# D-035 — Missing runnable seams: schedules, ELN cursor persistence, approval + skill-role seams

**Context.** The review found subsystems that were built and worker-registered but could not
actually run as designed, plus two Phase-6 seams worth landing early.

**Temporal Schedules (`scripts/schedules.py`, `make schedules-apply`).** The ELN sync and the
three memory-synthesis workflows documented themselves as Schedule-driven, but no
`create_schedule` call existed anywhere — they were unrunnable on a cadence. `planned_schedules()`
is the pure, testable list of what is maintained; `apply_schedules` creates each Schedule or
updates it in place (idempotent). Intervals are config (`*_schedule_minutes`).

**ELN sync cursor persistence (`eln.cursor`, `sync_cursors` table).** `ElnSyncWorkflow` required a
mandatory `since` with no caller and nothing fed `next_cursor` back. It is now self-cursoring:
started with no `since` (the scheduled case) it loads its high-water mark from `sync_cursors`,
syncs, and stores the advanced value via two new activities (`load_sync_cursor`/`store_sync_cursor`,
registered on the background worker). An explicit `since` (manual backfill) runs without touching
the stored cursor. Durability stays in Temporal + Postgres, per the layer rules.

**Approval starter/decider seam (`agents.interaction_tools`).** `InteractionApprovalWorkflow`
(D-032) had no in-repo starter. `start_approval`/`decide_approval`/`approval_status` are the one
working reference caller a chat UI hooks onto — mirroring the `qm_tools` client pattern, stable
`approval-<interaction_id>` id (idempotent surface), clear errors on an unknown hold.

**Phase-6 code-side seams.** `build_agent(actor=…)` threads an actor through the audit trail
(D-034), and `build_agent(allowed_skills=…)` + `agents.skill_access.RoleFilteredSkillsSource`
scope which skills the agent advertises — both default to today's behavior (`"unknown"` /
all-skills-visible), so Phase 6 is a value change at the call site, not new surgery. MCP auth,
Temporal mTLS, namespaces, and the HPC bridge remain true Phase-6 work (need live infra).

**Result.** New tests: `test_schedules` (plan coverage + config intervals), `test_cursor`
(pg-backed round-trip), `test_interaction_tools` (server-backed start/signal/query), plus
worker-registration assertions and `test_skill_access` (filter/pass-through/fail-closed).
`make lint type` green; `make test` green offline (server/pg cases run in CI).
